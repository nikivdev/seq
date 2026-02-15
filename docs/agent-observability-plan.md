# Agent Observability Plan

How seq + Hive + ClickHouse can transform the way you write code with Claude Code and Codex.

## The Problem

Both Claude Code and Codex produce rich event streams — every tool call, every model invocation, every token count, every error — but this data sits in flat JSONL files, never aggregated, never queried, never correlated.

### What they log today

**Claude Code** (`~/.claude/projects/<hash>/<session>.jsonl`):
- Every message: user input, assistant response, tool_use, tool_result
- Token usage per API call: `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`
- Model name, request ID, stop reason
- Thinking content (with signatures)
- Bash progress events with elapsed time
- File history snapshots for undo
- SQLite DB (`__store.db`): `cost_usd`, `duration_ms` per assistant message

**Codex** (`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`):
- Session metadata: cwd, model (`gpt-5.3-codex`), git commit/branch, personality
- `response_item`: function calls (`exec_command`), messages (`output_text`), reasoning (encrypted)
- `event_msg`: `agent_reasoning`, `agent_message`, `token_count`
- Token tracking per turn: `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`
- Rate limit state: `used_percent`, `window_minutes`, `resets_at`, `model_context_window`
- Turn context: approval policy, sandbox policy, collaboration mode

**Neither tool knows:**
- What you're doing in the other tool
- What app you're looking at on screen
- Whether you're AFK or deep in flow
- How long you spent reading docs vs writing code vs waiting for AI
- Which approach actually worked when you tried the same thing in both tools

## The Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    ClickHouse (local)                    │
│  agent.sessions   agent.turns   agent.tool_calls        │
│  agent.tokens     seq.context   hive.*                  │
│  ──── materialized views (auto-updating) ────           │
│  agent.cost_daily   agent.cache_efficiency              │
│  agent.tool_success_rate   agent.session_productivity   │
└──────▲──────────────────────▲──────────────▲────────────┘
       │                      │              │
  ingest daemon          libseqch.dylib   context poller
       │                      │              │
┌──────┴──────┐    ┌─────────┴──────┐  ┌────┴─────────┐
│  seqd-agent │    │   Hive agent   │  │    seqd       │
│  JSONL tail │    │   workflows    │  │  AFK/context  │
│  Claude +   │    │   MLX routing  │  │  OCR/frames   │
│  Codex logs │    │   tool calls   │  │  window poll  │
└─────────────┘    └────────────────┘  └──────────────┘
       ▲                                       ▲
       │                                       │
~/.claude/**/*.jsonl                   Accessibility API
~/.codex/sessions/**/*.jsonl           ScreenCaptureKit
```

## Phase 1: Ingest Agent Sessions into ClickHouse

Tail Claude Code and Codex JSONL files, parse events, push to ClickHouse via `libseqch.dylib`.

### New ClickHouse Tables

```sql
CREATE DATABASE IF NOT EXISTS agent;

-- Every coding agent session (Claude Code or Codex)
CREATE TABLE agent.sessions (
    ts_ms           UInt64,
    session_id      String,
    agent           LowCardinality(String),  -- 'claude' or 'codex'
    model           LowCardinality(String),  -- 'claude-opus-4-6', 'gpt-5.3-codex'
    project_path    String,
    git_branch      String,
    git_commit      String,
    dur_ms          UInt64,
    turns           UInt32,
    total_input_tokens   UInt64,
    total_output_tokens  UInt64,
    total_cost_usd  Float64
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (agent, session_id, ts_ms);

-- Every model call (unified across Claude + Codex)
CREATE TABLE agent.turns (
    ts_ms           UInt64,
    session_id      String,
    turn_id         String,
    agent           LowCardinality(String),
    model           LowCardinality(String),
    input_tokens    UInt32,
    output_tokens   UInt32,
    cached_tokens   UInt32,
    reasoning_tokens UInt32,
    dur_ms          UInt32,
    cost_usd        Float64,
    stop_reason     LowCardinality(String),
    is_error        UInt8,
    context_window  UInt32,        -- model's context limit
    context_used_pct Float32       -- how full the context was
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (agent, session_id, ts_ms);

-- Every tool execution (Bash, Read, Edit, Grep, exec_command, etc.)
CREATE TABLE agent.tool_calls (
    ts_ms           UInt64,
    session_id      String,
    turn_id         String,
    agent           LowCardinality(String),
    tool_name       LowCardinality(String),  -- 'Bash', 'Read', 'Edit', 'exec_command'
    input_summary   String,                   -- truncated command/path
    dur_ms          UInt32,
    ok              UInt8,
    output_lines    UInt32,
    output_bytes    UInt32
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (tool_name, ts_ms);

-- Token spend aggregated per model per hour (auto-updated)
CREATE TABLE agent.cost_hourly (
    agent           LowCardinality(String),
    model           LowCardinality(String),
    hour            DateTime,
    input_tokens    UInt64,
    output_tokens   UInt64,
    cached_tokens   UInt64,
    cost_usd        Float64,
    calls           UInt32
)
ENGINE = SummingMergeTree
ORDER BY (agent, model, hour);

CREATE MATERIALIZED VIEW agent.mv_cost_hourly TO agent.cost_hourly AS
SELECT
    agent, model,
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    input_tokens, output_tokens, cached_tokens,
    cost_usd, 1 AS calls
FROM agent.turns;
```

### Ingest Daemon

A lightweight process (Swift or C++) that:

1. Uses `FSEvents` to watch `~/.claude/projects/` and `~/.codex/sessions/` for new/modified JSONL files
2. Maintains a high-water mark per file (byte offset of last processed line)
3. Parses each new line, extracts the relevant fields
4. Pushes rows to ClickHouse via `libseqch.dylib` (same async batch writer used by seqd and Hive)

**Claude Code parsing** — for each JSONL line:
- `type: "assistant"` with `message.usage` → `agent.turns` row (extract token counts, model, cost)
- `type: "assistant"` with `message.content[].type == "tool_use"` → `agent.tool_calls` row
- `type: "user"` with `toolUseResult` → update corresponding tool_call with output/duration
- `type: "progress"` with `data.type == "bash_progress"` → update tool duration
- First/last message timestamps → `agent.sessions` row

**Codex parsing** — for each JSONL line:
- `type: "event_msg"` with `payload.type == "token_count"` → `agent.turns` row
- `type: "response_item"` with `payload.type == "function_call"` → `agent.tool_calls` row
- `type: "response_item"` with `payload.type == "function_call_output"` → update tool output
- `type: "session_meta"` → `agent.sessions` row
- `type: "turn_context"` → extract context window usage

## Phase 2: Correlate Agent Activity with Mac Context

Join agent events with seq context data to understand what you were doing when you invoked each tool.

### Queries

```sql
-- What app were you in when you started each agent session?
SELECT
    s.agent,
    s.model,
    c.app AS started_from_app,
    count() AS sessions
FROM agent.sessions s
ASOF JOIN seq.context c ON s.ts_ms >= c.ts_ms
WHERE s.ts_ms > toUnixTimestamp(now() - INTERVAL 7 DAY) * 1000
GROUP BY s.agent, s.model, started_from_app
ORDER BY sessions DESC;

-- Coding time vs AI time per hour
SELECT
    toStartOfHour(toDateTime(c.ts_ms / 1000)) AS hour,
    round(sumIf(c.dur_ms, c.app IN ('Xcode', 'Cursor', 'Zed', 'Terminal')) / 60000, 1) AS coding_min,
    round(sumIf(c.dur_ms, c.app IN ('Claude', 'Codex')) / 60000, 1) AS ai_ui_min,
    (SELECT sum(dur_ms) / 60000 FROM agent.turns t
     WHERE toStartOfHour(toDateTime(t.ts_ms / 1000)) = hour) AS ai_compute_min
FROM seq.context c
WHERE c.ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour ORDER BY hour;

-- Which agent do you prefer for which project?
SELECT
    s.project_path,
    s.agent,
    count() AS sessions,
    sum(s.total_input_tokens + s.total_output_tokens) AS total_tokens,
    round(sum(s.total_cost_usd), 4) AS total_cost
FROM agent.sessions s
WHERE s.ts_ms > toUnixTimestamp(now() - INTERVAL 30 DAY) * 1000
GROUP BY s.project_path, s.agent
ORDER BY s.project_path, total_tokens DESC;
```

## Phase 3: Smart Context Injection via Hive

Use Hive agent graphs to read ClickHouse analytics and inject relevant context into Claude/Codex sessions.

### Context Pre-warming

Before you start a coding session, a Hive agent queries ClickHouse for:

1. **Recent session history for this project** — what files were touched, what tools failed, what the last error was
2. **Cross-agent context** — "You asked Codex about X 2 hours ago. Here's what it found."
3. **Screen context** — "You have `auth.rs:142` open in Xcode right now"

This context gets written to:
- `CLAUDE.md` memory files for Claude Code
- `AGENTS.md` or skill files for Codex

```swift
// Hive node that queries ClickHouse and updates agent memory
Node<ContextSchema>("refresh_agent_memory") { input in
    let project = try input.store.get(ContextSchema.activeProject)

    // Query recent sessions for this project
    let recentSessions = try await clickhouse.query("""
        SELECT agent, model, session_id,
               total_input_tokens + total_output_tokens AS tokens
        FROM agent.sessions
        WHERE project_path = '\(project)'
        ORDER BY ts_ms DESC LIMIT 5
    """)

    // Query recent tool failures
    let recentErrors = try await clickhouse.query("""
        SELECT tool_name, input_summary, count() AS failures
        FROM agent.tool_calls
        WHERE session_id IN (
            SELECT session_id FROM agent.sessions
            WHERE project_path = '\(project)'
            AND ts_ms > toUnixTimestamp(now() - INTERVAL 1 HOUR) * 1000
        ) AND ok = 0
        GROUP BY tool_name, input_summary
        ORDER BY failures DESC LIMIT 3
    """)

    // Write to memory file
    let memory = formatMemory(sessions: recentSessions, errors: recentErrors)
    try memory.write(toFile: "\(project)/.claude/projects/.../memory/session-context.md")

    return Effects {
        Set(ContextSchema.lastRefresh, Date().timeIntervalSince1970)
        GoTo("monitor_context")
    }
}
```

### Smart Model Routing

Use ClickHouse data to decide when to use local MLX vs cloud:

```swift
Node<RouterSchema>("decide_model") { input in
    let task = try input.store.get(RouterSchema.currentTask)

    // Check token budget remaining (from ClickHouse rate limit data)
    let todaySpend = try await clickhouse.query("""
        SELECT sum(cost_usd) FROM agent.cost_hourly
        WHERE hour >= toStartOfDay(now())
    """)

    // Check if similar task was done recently (avoid duplicate work)
    let recentSimilar = try await clickhouse.query("""
        SELECT agent, model, session_id FROM agent.sessions
        WHERE project_path = '\(project)'
        AND ts_ms > toUnixTimestamp(now() - INTERVAL 1 HOUR) * 1000
        ORDER BY ts_ms DESC LIMIT 1
    """)

    if todaySpend > budgetLimit {
        return Effects { Set(RouterSchema.selectedModel, "mlx:qwen-7b") }
    }
    if task.isPrivate {
        return Effects { Set(RouterSchema.selectedModel, "mlx:qwen-7b") }
    }
    return Effects { Set(RouterSchema.selectedModel, "claude-opus-4-6") }
}
```

## Phase 4: Cross-Agent Session Continuity

When you switch from Codex to Claude (or vice versa) on the same project, automatically carry context forward.

### How It Works

1. Hive monitors `seq.context` for app switches to/from Terminal (where both agents run)
2. When a new Claude or Codex session starts (detected via JSONL file creation), Hive:
   - Queries the last session from the other agent on the same project
   - Extracts: files modified, key decisions made, unresolved errors
   - Formats a summary and injects it via memory/AGENTS.md

### Continuity Query

```sql
-- Get the last session's key context for handoff
SELECT
    t.tool_name,
    t.input_summary,
    t.ok,
    count() AS calls
FROM agent.tool_calls t
JOIN agent.sessions s ON t.session_id = s.session_id
WHERE s.project_path = '/Users/nikiv/code/flow'
  AND s.agent = 'codex'  -- last agent used
  AND s.ts_ms = (
      SELECT max(ts_ms) FROM agent.sessions
      WHERE project_path = '/Users/nikiv/code/flow' AND agent = 'codex'
  )
GROUP BY t.tool_name, t.input_summary, t.ok
ORDER BY calls DESC;
```

## Phase 5: Analytics Dashboard Queries

### Daily Cost Report

```sql
SELECT
    agent,
    model,
    sum(input_tokens) AS input_tok,
    sum(output_tokens) AS output_tok,
    sum(cached_tokens) AS cached_tok,
    round(sum(cached_tokens) * 100.0 / nullIf(sum(input_tokens), 0), 1) AS cache_hit_pct,
    round(sum(cost_usd), 4) AS cost_usd,
    count() AS api_calls
FROM agent.turns
WHERE ts_ms > toUnixTimestamp(toStartOfDay(now())) * 1000
GROUP BY agent, model
ORDER BY cost_usd DESC;
```

### Cache Efficiency (Claude's prompt caching vs Codex's caching)

```sql
SELECT
    agent,
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    avg(cached_tokens * 100.0 / nullIf(input_tokens, 0)) AS avg_cache_pct,
    count() AS calls
FROM agent.turns
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY agent, hour
ORDER BY hour;
```

### Tool Success Rate

```sql
SELECT
    agent,
    tool_name,
    count() AS calls,
    countIf(ok = 1) AS successes,
    round(countIf(ok = 1) * 100.0 / count(), 1) AS success_pct,
    avg(dur_ms) AS avg_ms
FROM agent.tool_calls
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 7 DAY) * 1000
GROUP BY agent, tool_name
ORDER BY calls DESC;
```

### Session Productivity (tokens per minute of coding)

```sql
WITH coding_hours AS (
    SELECT
        toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
        sum(dur_ms) / 60000 AS coding_min
    FROM seq.context
    WHERE app IN ('Xcode', 'Cursor', 'Zed', 'Terminal', 'iTerm2')
      AND ts_ms > toUnixTimestamp(now() - INTERVAL 7 DAY) * 1000
    GROUP BY hour
),
agent_hours AS (
    SELECT
        toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
        sum(output_tokens) AS tokens
    FROM agent.turns
    WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 7 DAY) * 1000
    GROUP BY hour
)
SELECT
    c.hour,
    c.coding_min,
    a.tokens,
    round(a.tokens / nullIf(c.coding_min, 0), 0) AS tokens_per_coding_min
FROM coding_hours c
JOIN agent_hours a ON c.hour = a.hour
ORDER BY c.hour;
```

## Implementation Order

1. **ClickHouse schema** — add `agent.*` tables to `seqmem.sql`
2. **C ABI extension** — add `seq_ch_push_agent_turn`, `seq_ch_push_agent_tool_call` to `libseqch.dylib`
3. **Ingest daemon** — Swift actor that tails JSONL files via FSEvents, parses, pushes to ClickHouse
4. **Hive agent** — context refresh graph that queries ClickHouse and updates memory files
5. **Codex app-server bridge** — Hive tool that spawns `codex app-server` for `review/start` integration
6. **Cross-agent handoff** — detect agent switch, query last session, inject summary

## What This Unlocks

- **"How much did AI help me today?"** — real cost/token/time data across both tools
- **"Which tool is better for what?"** — per-project, per-task comparison
- **"Why did that session fail?"** — correlate tool errors with context (what app, what file, what time)
- **"Don't repeat yourself"** — if Codex already explored a path, Claude knows about it
- **"Am I using the cache well?"** — track prompt caching efficiency over time
- **"Budget awareness"** — route to local MLX when cloud spend is high
- **"Context-aware sessions"** — start each session with knowledge of what just happened
