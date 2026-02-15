# Agent Observability — Implementation Spec

Step-by-step guide to implement the agent observability system from `agent-observability-plan.md`. Each step lists exact files to modify, code to write, and how to test.

## Prerequisites

- ClickHouse running locally (`./tools/clickhouse/local_server.sh start`)
- `libseqch.dylib` built (`sh cli/cpp/run.sh build`)
- Hive repo at `~/repos/christopherkarani/Hive/Sources/Hive`

## Step 1: ClickHouse Schema — `agent.*` Tables

**File:** `~/code/seq/tools/clickhouse/seqmem.sql`

Append after the `hive.mv_tool_error_hotspots` materialized view (line 197):

```sql
-- ─── Agent coding assistant tables ────────────────────────────────────────

CREATE DATABASE IF NOT EXISTS agent;

-- Every coding agent session (Claude Code or Codex)
CREATE TABLE IF NOT EXISTS agent.sessions (
    ts_ms           UInt64,
    session_id      String,
    agent           LowCardinality(String),  -- 'claude' or 'codex'
    model           LowCardinality(String),
    project_path    String,
    git_branch      String DEFAULT '',
    git_commit      String DEFAULT '',
    dur_ms          UInt64 DEFAULT 0,
    turns           UInt32 DEFAULT 0,
    total_input_tokens   UInt64 DEFAULT 0,
    total_output_tokens  UInt64 DEFAULT 0,
    total_cost_usd  Float64 DEFAULT 0
)
ENGINE = ReplacingMergeTree(ts_ms)
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (agent, session_id);

-- Every model turn (unified across Claude + Codex)
CREATE TABLE IF NOT EXISTS agent.turns (
    ts_ms           UInt64,
    session_id      String,
    turn_index      UInt32,
    agent           LowCardinality(String),
    model           LowCardinality(String),
    input_tokens    UInt32 DEFAULT 0,
    output_tokens   UInt32 DEFAULT 0,
    cached_tokens   UInt32 DEFAULT 0,
    reasoning_tokens UInt32 DEFAULT 0,
    dur_ms          UInt32 DEFAULT 0,
    cost_usd        Float64 DEFAULT 0,
    stop_reason     LowCardinality(String) DEFAULT '',
    is_error        UInt8 DEFAULT 0,
    context_window  UInt32 DEFAULT 0,
    context_used_pct Float32 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (agent, session_id, ts_ms);

-- Every tool execution (Bash, Read, Edit, exec_command, etc.)
CREATE TABLE IF NOT EXISTS agent.tool_calls (
    ts_ms           UInt64,
    session_id      String,
    turn_index      UInt32,
    agent           LowCardinality(String),
    tool_name       LowCardinality(String),
    input_summary   String DEFAULT '',
    dur_ms          UInt32 DEFAULT 0,
    ok              UInt8 DEFAULT 1,
    output_lines    UInt32 DEFAULT 0,
    output_bytes    UInt32 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (tool_name, ts_ms);

-- Token cost per agent per model per hour (auto-updated)
CREATE TABLE IF NOT EXISTS agent.cost_hourly (
    agent           LowCardinality(String),
    model           LowCardinality(String),
    hour            DateTime,
    input_tokens    UInt64,
    output_tokens   UInt64,
    cached_tokens   UInt64,
    cost_usd        Float64,
    calls           UInt64
)
ENGINE = SummingMergeTree
ORDER BY (agent, model, hour);

CREATE MATERIALIZED VIEW IF NOT EXISTS agent.mv_cost_hourly TO agent.cost_hourly AS
SELECT
    agent, model,
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    input_tokens, output_tokens, cached_tokens,
    cost_usd, 1 AS calls
FROM agent.turns;

-- Tool success rate (auto-updated)
CREATE TABLE IF NOT EXISTS agent.tool_success (
    agent           LowCardinality(String),
    tool_name       LowCardinality(String),
    day             Date,
    calls           UInt64,
    failures        UInt64
)
ENGINE = SummingMergeTree
ORDER BY (agent, tool_name, day);

CREATE MATERIALIZED VIEW IF NOT EXISTS agent.mv_tool_success TO agent.tool_success AS
SELECT
    agent, tool_name,
    toDate(toDateTime(ts_ms / 1000)) AS day,
    1 AS calls,
    if(ok = 0, 1, 0) AS failures
FROM agent.tool_calls;
```

**Why `ReplacingMergeTree` for sessions:** Sessions are updated incrementally as new JSONL lines arrive. Each time the ingest daemon sees a new event for a session, it upserts with updated aggregates (total tokens, cost, duration). `ReplacingMergeTree(ts_ms)` keeps only the latest version.

**Test:**

```bash
# Apply schema
./tools/clickhouse/seqmem_setup.sh

# Verify tables exist
clickhouse-client --query "SHOW TABLES FROM agent"
# Expected: cost_hourly, mv_cost_hourly, mv_tool_success, sessions, tool_calls, tool_success, turns
```

---

## Step 2: C ABI Extension — New Push Functions

Three new row types and three new C ABI functions. Follow the existing pattern in `clickhouse.h` → `clickhouse.cpp` → `clickhouse_bridge.h` → `clickhouse_bridge.cpp`.

### 2a. `clickhouse.h` — Add Row Types + Methods

After `ToolCallRow` (line 96), add:

```cpp
struct AgentSessionRow {
    uint64_t ts_ms = 0;
    std::string session_id;
    std::string agent;         // "claude" or "codex"
    std::string model;
    std::string project_path;
    std::string git_branch;
    std::string git_commit;
    uint64_t dur_ms = 0;
    uint32_t turns = 0;
    uint64_t total_input_tokens = 0;
    uint64_t total_output_tokens = 0;
    double total_cost_usd = 0.0;
};

struct AgentTurnRow {
    uint64_t ts_ms = 0;
    std::string session_id;
    uint32_t turn_index = 0;
    std::string agent;
    std::string model;
    uint32_t input_tokens = 0;
    uint32_t output_tokens = 0;
    uint32_t cached_tokens = 0;
    uint32_t reasoning_tokens = 0;
    uint32_t dur_ms = 0;
    double cost_usd = 0.0;
    std::string stop_reason;
    uint8_t is_error = 0;
    uint32_t context_window = 0;
    float context_used_pct = 0.0f;
};

struct AgentToolCallRow {
    uint64_t ts_ms = 0;
    std::string session_id;
    uint32_t turn_index = 0;
    std::string agent;
    std::string tool_name;
    std::string input_summary;
    uint32_t dur_ms = 0;
    uint8_t ok = 1;
    uint32_t output_lines = 0;
    uint32_t output_bytes = 0;
};
```

In `Client` class, add:

```cpp
size_t InsertAgentSessions(std::span<const AgentSessionRow> rows);
size_t InsertAgentTurns(std::span<const AgentTurnRow> rows);
size_t InsertAgentToolCalls(std::span<const AgentToolCallRow> rows);
```

In `AsyncWriter` class, add:

```cpp
void PushAgentSession(AgentSessionRow row) noexcept;
void PushAgentTurn(AgentTurnRow row) noexcept;
void PushAgentToolCall(AgentToolCallRow row) noexcept;
```

And in the private section of `AsyncWriter`:

```cpp
std::vector<AgentSessionRow> agent_session_pending_;
std::vector<AgentTurnRow> agent_turn_pending_;
std::vector<AgentToolCallRow> agent_tool_pending_;
```

### 2b. `clickhouse.cpp` — Insert Implementations

Follow the pattern of `InsertToolCalls`. For `InsertAgentTurns` (the most complex):

```cpp
size_t Client::InsertAgentTurns(std::span<const AgentTurnRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_session_id = std::make_shared<clickhouse::ColumnString>();
    auto col_turn_index = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_agent = std::make_shared<clickhouse::ColumnString>();
    auto col_model = std::make_shared<clickhouse::ColumnString>();
    auto col_input_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_output_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_cached_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_reasoning_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_dur_ms = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_cost_usd = std::make_shared<clickhouse::ColumnFloat64>();
    auto col_stop_reason = std::make_shared<clickhouse::ColumnString>();
    auto col_is_error = std::make_shared<clickhouse::ColumnUInt8>();
    auto col_context_window = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_context_used_pct = std::make_shared<clickhouse::ColumnFloat32>();

    for (const auto& row : rows) {
        col_ts_ms->Append(row.ts_ms);
        col_session_id->Append(row.session_id);
        col_turn_index->Append(row.turn_index);
        col_agent->Append(row.agent);
        col_model->Append(row.model);
        col_input_tokens->Append(row.input_tokens);
        col_output_tokens->Append(row.output_tokens);
        col_cached_tokens->Append(row.cached_tokens);
        col_reasoning_tokens->Append(row.reasoning_tokens);
        col_dur_ms->Append(row.dur_ms);
        col_cost_usd->Append(row.cost_usd);
        col_stop_reason->Append(row.stop_reason);
        col_is_error->Append(row.is_error);
        col_context_window->Append(row.context_window);
        col_context_used_pct->Append(row.context_used_pct);
    }

    clickhouse::Block block;
    block.AppendColumn("ts_ms", col_ts_ms);
    block.AppendColumn("session_id", col_session_id);
    block.AppendColumn("turn_index", col_turn_index);
    block.AppendColumn("agent", col_agent);
    block.AppendColumn("model", col_model);
    block.AppendColumn("input_tokens", col_input_tokens);
    block.AppendColumn("output_tokens", col_output_tokens);
    block.AppendColumn("cached_tokens", col_cached_tokens);
    block.AppendColumn("reasoning_tokens", col_reasoning_tokens);
    block.AppendColumn("dur_ms", col_dur_ms);
    block.AppendColumn("cost_usd", col_cost_usd);
    block.AppendColumn("stop_reason", col_stop_reason);
    block.AppendColumn("is_error", col_is_error);
    block.AppendColumn("context_window", col_context_window);
    block.AppendColumn("context_used_pct", col_context_used_pct);

    impl_->client->Insert("agent.turns", block);
    return rows.size();
}
```

`InsertAgentSessions` and `InsertAgentToolCalls` follow the same column-per-field pattern.

In `AsyncWriter::PushAgentTurn`:

```cpp
void AsyncWriter::PushAgentTurn(AgentTurnRow row) noexcept {
    std::lock_guard lk(mu_);
    agent_turn_pending_.push_back(std::move(row));
    cv_.notify_one();
}
```

In `AsyncWriter::DrainAndInsert`, add alongside existing drain blocks:

```cpp
std::vector<AgentSessionRow> agent_sessions;
std::vector<AgentTurnRow> agent_turns;
std::vector<AgentToolCallRow> agent_tools;
{
    std::lock_guard lk(mu_);
    agent_sessions.swap(agent_session_pending_);
    agent_turns.swap(agent_turn_pending_);
    agent_tools.swap(agent_tool_pending_);
}
if (!agent_sessions.empty())
    inserted_count_ += client.InsertAgentSessions(agent_sessions);
if (!agent_turns.empty())
    inserted_count_ += client.InsertAgentTurns(agent_turns);
if (!agent_tools.empty())
    inserted_count_ += client.InsertAgentToolCalls(agent_tools);
```

### 2c. `clickhouse_bridge.h` — C ABI Declarations

Append after the `seq_ch_push_tool_call` declaration:

```c
/* ── Agent coding assistant events ─────────────────────────────────────── */

void seq_ch_push_agent_session(seq_ch_writer_t* w,
                                uint64_t ts_ms,
                                const char* session_id,
                                const char* agent,
                                const char* model,
                                const char* project_path,
                                const char* git_branch,
                                const char* git_commit,
                                uint64_t dur_ms,
                                uint32_t turns,
                                uint64_t total_input_tokens,
                                uint64_t total_output_tokens,
                                double total_cost_usd);

void seq_ch_push_agent_turn(seq_ch_writer_t* w,
                             uint64_t ts_ms,
                             const char* session_id,
                             uint32_t turn_index,
                             const char* agent,
                             const char* model,
                             uint32_t input_tokens,
                             uint32_t output_tokens,
                             uint32_t cached_tokens,
                             uint32_t reasoning_tokens,
                             uint32_t dur_ms,
                             double cost_usd,
                             const char* stop_reason,
                             uint8_t is_error,
                             uint32_t context_window,
                             float context_used_pct);

void seq_ch_push_agent_tool_call(seq_ch_writer_t* w,
                                  uint64_t ts_ms,
                                  const char* session_id,
                                  uint32_t turn_index,
                                  const char* agent,
                                  const char* tool_name,
                                  const char* input_summary,
                                  uint32_t dur_ms,
                                  uint8_t ok,
                                  uint32_t output_lines,
                                  uint32_t output_bytes);
```

### 2d. `clickhouse_bridge.cpp` — C ABI Implementations

Follow the exact pattern of existing push functions (NULL-check `w`, NULL-check each string, call `w->writer.PushX()`).

**Test:**

```bash
cd ~/code/seq
rm -f cli/cpp/out/build/ch/libseqch.dylib
sh cli/cpp/run.sh build

# Verify new symbols are exported
nm -gU cli/cpp/out/bin/libseqch.dylib | grep agent
# Expected: _seq_ch_push_agent_session, _seq_ch_push_agent_turn, _seq_ch_push_agent_tool_call
```

---

## Step 3: Ingest Daemon — Swift Actor

A new Swift executable target that tails Claude Code and Codex JSONL files and pushes parsed events to ClickHouse.

### 3a. Location

New Hive target: `~/repos/christopherkarani/Hive/Sources/Hive/Sources/HiveAgentIngest/`

Files:
- `AgentIngestDaemon.swift` — main actor (FSEvents watcher + JSONL parser)
- `ClaudeCodeParser.swift` — Claude Code JSONL line parser
- `CodexParser.swift` — Codex JSONL line parser
- `ClickHousePusher.swift` — dlopen bridge to libseqch (like HiveClickHouseSink)

New executable: `~/repos/christopherkarani/Hive/Sources/Hive/Examples/AgentIngestDaemon/main.swift`

**Safety default (important):** the daemon defaults to *not* backfilling historical JSONL. It will prime offsets to EOF for any JSONL file it hasn't seen before, then ingest only new appended lines. Use `--backfill` if you explicitly want to ingest your full history.

### 3b. Package.swift Changes

Add to `Package.swift`:

```swift
.target(
    name: "HiveAgentIngest",
    dependencies: [],
    path: "Sources/HiveAgentIngest"
),
.executableTarget(
    name: "HiveAgentIngestDaemon",
    dependencies: ["HiveAgentIngest"],
    path: "Examples/AgentIngestDaemon"
),
```

No external dependencies needed — just Foundation for JSON parsing and FSEvents via CoreServices.

### 3c. Core Architecture

```swift
// AgentIngestDaemon.swift

import Foundation
import CoreServices  // FSEvents

public actor AgentIngestDaemon {
    /// Byte offset per file (high-water mark for incremental tailing)
    private var offsets: [String: UInt64] = [:]

    /// Paths to watch
    private let claudeProjectsDir: String  // ~/.claude/projects/
    private let codexSessionsDir: String   // ~/.codex/sessions/

    /// ClickHouse pusher
    private let pusher: ClickHousePusher

    /// Session accumulators (session_id → running totals)
    private var sessions: [String: SessionAccumulator] = [:]

    public init(
        claudeDir: String = "\(NSHomeDirectory())/.claude/projects",
        codexDir: String = "\(NSHomeDirectory())/.codex/sessions",
        dylibPath: String? = nil
    ) {
        self.claudeProjectsDir = claudeDir
        self.codexSessionsDir = codexDir
        self.pusher = ClickHousePusher(dylibPath: dylibPath)
    }

    public func run() async throws {
        // 1. Do initial scan of existing files (catch up on anything missed)
        await scanExisting(dir: claudeProjectsDir, parser: .claude)
        await scanExisting(dir: codexSessionsDir, parser: .codex)

        // 2. Start FSEvents watchers
        let stream = startFSEvents(paths: [claudeProjectsDir, codexSessionsDir])

        // 3. Process file change events
        for await changedPath in stream {
            if changedPath.hasSuffix(".jsonl") {
                let parser: ParserKind = changedPath.contains("/.claude/") ? .claude : .codex
                await tailFile(path: changedPath, parser: parser)
            }
        }
    }
}
```

### 3d. FSEvents Watcher

```swift
/// Wraps CoreServices FSEventStream as an AsyncStream
func startFSEvents(paths: [String]) -> AsyncStream<String> {
    AsyncStream { continuation in
        let callback: FSEventStreamCallback = { _, info, numEvents, eventPaths, _, _ in
            let cont = Unmanaged<Continuation>.fromOpaque(info!).takeUnretainedValue()
            let paths = eventPaths.assumingMemoryBound(to: UnsafePointer<CChar>.self)
            for i in 0..<numEvents {
                cont.yield(String(cString: paths[i]))
            }
        }

        var context = FSEventStreamContext()
        let cont = Continuation(continuation)
        context.info = Unmanaged.passRetained(cont).toOpaque()

        let stream = FSEventStreamCreate(
            nil, callback, &context,
            paths as CFArray,
            FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
            0.5,  // 500ms latency (batch file events)
            UInt32(kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagUseCFTypes)
        )!

        FSEventStreamScheduleWithRunLoop(stream, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
        FSEventStreamStart(stream)
    }
}
```

### 3e. JSONL Tail Logic

```swift
/// Read new lines from a JSONL file since last known offset
func tailFile(path: String, parser: ParserKind) async {
    let offset = offsets[path] ?? 0
    guard let handle = FileHandle(forReadingAtPath: path) else { return }
    defer { try? handle.close() }

    handle.seek(toFileOffset: offset)
    let data = handle.readDataToEndOfFile()
    guard !data.isEmpty else { return }

    offsets[path] = offset + UInt64(data.count)

    // Split into lines, parse each
    let lines = data.split(separator: UInt8(ascii: "\n"))
    for lineData in lines {
        guard let json = try? JSONSerialization.jsonObject(with: Data(lineData)) as? [String: Any] else {
            continue
        }
        switch parser {
        case .claude:
            ClaudeCodeParser.parse(json: json, sessionID: sessionID(from: path), pusher: pusher)
        case .codex:
            CodexParser.parse(json: json, sessionID: sessionID(from: path), pusher: pusher)
        }
    }
}

/// Extract session ID from file path
/// Claude: ~/.claude/projects/<hash>/<session-id>.jsonl → session-id
/// Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl → uuid
func sessionID(from path: String) -> String {
    let filename = (path as NSString).lastPathComponent
    return (filename as NSString).deletingPathExtension
}
```

### 3f. Claude Code Parser

```swift
// ClaudeCodeParser.swift

enum ClaudeCodeParser {
    /// Track turn index per session
    private static var turnCounters: [String: UInt32] = [:]

    static func parse(json: [String: Any], sessionID: String, pusher: ClickHousePusher) {
        guard let type = json["type"] as? String else { return }
        let ts = (json["timestamp"] as? String).flatMap { ISO8601DateFormatter().date(from: $0) }
        let tsMs = UInt64((ts?.timeIntervalSince1970 ?? Date().timeIntervalSince1970) * 1000)

        switch type {
        case "assistant":
            guard let message = json["message"] as? [String: Any] else { return }

            // Extract token usage
            if let usage = message["usage"] as? [String: Any] {
                let turnIndex = turnCounters[sessionID, default: 0]
                turnCounters[sessionID] = turnIndex + 1

                let inputTokens = usage["input_tokens"] as? UInt32 ?? 0
                let outputTokens = usage["output_tokens"] as? UInt32 ?? 0
                let cacheCreation = usage["cache_creation_input_tokens"] as? UInt32 ?? 0
                let cacheRead = usage["cache_read_input_tokens"] as? UInt32 ?? 0
                let model = message["model"] as? String ?? ""
                let stopReason = message["stop_reason"] as? String ?? ""

                pusher.pushAgentTurn(
                    tsMs: tsMs,
                    sessionID: sessionID,
                    turnIndex: turnIndex,
                    agent: "claude",
                    model: model,
                    inputTokens: inputTokens,
                    outputTokens: outputTokens,
                    cachedTokens: cacheCreation + cacheRead,
                    reasoningTokens: 0,
                    durMs: 0,  // Updated later if progress events are available
                    costUsd: estimateCost(model: model, input: inputTokens, output: outputTokens,
                                          cacheCreation: cacheCreation, cacheRead: cacheRead),
                    stopReason: stopReason,
                    isError: stopReason == "error" ? 1 : 0,
                    contextWindow: 0,
                    contextUsedPct: 0
                )
            }

            // Extract tool_use from content array
            if let content = message["content"] as? [[String: Any]] {
                for item in content where item["type"] as? String == "tool_use" {
                    let toolName = item["name"] as? String ?? ""
                    var inputSummary = ""

                    // Truncate input to first 200 chars
                    if let input = item["input"] as? [String: Any] {
                        if let cmd = input["command"] as? String {
                            inputSummary = String(cmd.prefix(200))
                        } else if let path = input["file_path"] as? String {
                            inputSummary = path
                        } else if let pattern = input["pattern"] as? String {
                            inputSummary = pattern
                        }
                    }

                    pusher.pushAgentToolCall(
                        tsMs: tsMs,
                        sessionID: sessionID,
                        turnIndex: turnCounters[sessionID, default: 0] - 1,
                        agent: "claude",
                        toolName: toolName,
                        inputSummary: inputSummary,
                        durMs: 0,
                        ok: 1,
                        outputLines: 0,
                        outputBytes: 0
                    )
                }
            }

        case "user":
            // Check for tool results (contain output info)
            if let message = json["message"] as? [String: Any],
               let content = message["content"] as? [[String: Any]] {
                for item in content where item["type"] as? String == "tool_result" {
                    let isError = item["is_error"] as? Bool ?? false
                    // Could correlate back to tool_use via tool_use_id if needed
                    if isError {
                        // Update the most recent tool call as failed
                        // (simplified: in practice, match by tool_use_id)
                    }
                }
            }

        default:
            break
        }
    }
}
```

### 3g. Codex Parser

```swift
// CodexParser.swift

enum CodexParser {
    private static var turnCounters: [String: UInt32] = [:]

    static func parse(json: [String: Any], sessionID: String, pusher: ClickHousePusher) {
        guard let type = json["type"] as? String else { return }
        let tsMs = UInt64(Date().timeIntervalSince1970 * 1000)

        switch type {
        case "session_meta":
            guard let payload = json["payload"] as? [String: Any] else { return }
            let model = payload["model"] as? String ?? ""
            let cwd = payload["cwd"] as? String ?? ""
            let gitBranch = (payload["git"] as? [String: Any])?["branch"] as? String ?? ""
            let gitCommit = (payload["git"] as? [String: Any])?["commit"] as? String ?? ""

            pusher.pushAgentSession(
                tsMs: tsMs,
                sessionID: sessionID,
                agent: "codex",
                model: model,
                projectPath: cwd,
                gitBranch: gitBranch,
                gitCommit: gitCommit,
                durMs: 0, turns: 0,
                totalInputTokens: 0, totalOutputTokens: 0,
                totalCostUsd: 0
            )

        case "event_msg":
            guard let payload = json["payload"] as? [String: Any],
                  let payloadType = payload["type"] as? String else { return }

            if payloadType == "token_count",
               let info = payload["info"] as? [String: Any],
               let usage = info["total_token_usage"] as? [String: Any] {

                let turnIndex = turnCounters[sessionID, default: 0]
                turnCounters[sessionID] = turnIndex + 1

                let inputTokens = usage["input_tokens"] as? UInt32 ?? 0
                let outputTokens = usage["output_tokens"] as? UInt32 ?? 0
                let cachedTokens = usage["cached_input_tokens"] as? UInt32 ?? 0
                let reasoningTokens = usage["reasoning_output_tokens"] as? UInt32 ?? 0
                let contextWindow = (info["model_context_window"] as? NSNumber)?.uint32Value ?? 0

                var contextUsedPct: Float = 0
                if let limits = payload["rate_limits"] as? [String: Any],
                   let primary = limits["primary"] as? [String: Any],
                   let used = primary["used_percent"] as? Double {
                    contextUsedPct = Float(used)
                }

                pusher.pushAgentTurn(
                    tsMs: tsMs,
                    sessionID: sessionID,
                    turnIndex: turnIndex,
                    agent: "codex",
                    model: "",  // Codex model comes from session_meta
                    inputTokens: inputTokens,
                    outputTokens: outputTokens,
                    cachedTokens: cachedTokens,
                    reasoningTokens: reasoningTokens,
                    durMs: 0,
                    costUsd: 0,  // Codex doesn't expose per-turn cost
                    stopReason: "",
                    isError: 0,
                    contextWindow: contextWindow,
                    contextUsedPct: contextUsedPct
                )
            }

        case "response_item":
            guard let payload = json["payload"] as? [String: Any],
                  let payloadType = payload["type"] as? String else { return }

            if payloadType == "function_call" {
                let toolName = payload["name"] as? String ?? "exec_command"
                let args = payload["arguments"] as? String ?? ""
                let inputSummary = String(args.prefix(200))

                pusher.pushAgentToolCall(
                    tsMs: tsMs,
                    sessionID: sessionID,
                    turnIndex: turnCounters[sessionID, default: 0],
                    agent: "codex",
                    toolName: toolName,
                    inputSummary: inputSummary,
                    durMs: 0,
                    ok: 1,
                    outputLines: 0,
                    outputBytes: 0
                )
            }

            if payloadType == "function_call_output" {
                let output = payload["output"] as? String ?? ""
                // Most recent tool call gets updated with output info
                // (simplified: track by call_id for exact matching)
                _ = output.count  // output_bytes
                _ = output.components(separatedBy: "\n").count  // output_lines
            }

        default:
            break
        }
    }
}
```

### 3h. ClickHouse Pusher (dlopen Bridge)

Pattern identical to `HiveClickHouseSink.swift` — dlopen `libseqch.dylib`, resolve the three new C functions via `dlsym`.

```swift
// ClickHousePusher.swift

import Foundation

public final class ClickHousePusher: @unchecked Sendable {
    private let writer: OpaquePointer?

    // C ABI function types
    private typealias PushSessionFn = @convention(c) (
        OpaquePointer?, UInt64, UnsafePointer<CChar>?, UnsafePointer<CChar>?,
        UnsafePointer<CChar>?, UnsafePointer<CChar>?, UnsafePointer<CChar>?,
        UnsafePointer<CChar>?, UInt64, UInt32, UInt64, UInt64, Double
    ) -> Void
    private typealias PushTurnFn = @convention(c) (
        OpaquePointer?, UInt64, UnsafePointer<CChar>?, UInt32,
        UnsafePointer<CChar>?, UnsafePointer<CChar>?,
        UInt32, UInt32, UInt32, UInt32, UInt32, Double,
        UnsafePointer<CChar>?, UInt8, UInt32, Float
    ) -> Void
    private typealias PushToolCallFn = @convention(c) (
        OpaquePointer?, UInt64, UnsafePointer<CChar>?, UInt32,
        UnsafePointer<CChar>?, UnsafePointer<CChar>?, UnsafePointer<CChar>?,
        UInt32, UInt8, UInt32, UInt32
    ) -> Void
    private typealias FlushFn = @convention(c) (OpaquePointer?) -> Void

    private let pushSessionFn: PushSessionFn?
    private let pushTurnFn: PushTurnFn?
    private let pushToolCallFn: PushToolCallFn?
    private let flushFn: FlushFn?

    public init(dylibPath: String? = nil) {
        let searchPaths = [
            dylibPath,
            ProcessInfo.processInfo.environment["SEQ_CH_DYLIB_PATH"],
            "\(NSHomeDirectory())/code/seq/cli/cpp/out/bin/libseqch.dylib",
            "/usr/local/lib/libseqch.dylib"
        ].compactMap { $0 }

        var handle: UnsafeMutableRawPointer?
        for path in searchPaths {
            handle = dlopen(path, RTLD_NOW)
            if handle != nil { break }
        }

        guard let handle else {
            writer = nil; pushSessionFn = nil; pushTurnFn = nil
            pushToolCallFn = nil; flushFn = nil
            return
        }

        typealias CreateFn = @convention(c) (
            UnsafePointer<CChar>?, UInt16, UnsafePointer<CChar>?
        ) -> OpaquePointer?
        let createFn = unsafeBitCast(dlsym(handle, "seq_ch_writer_create"), to: CreateFn.self)
        writer = "127.0.0.1".withCString { host in
            "agent".withCString { db in createFn(host, 9000, db) }
        }

        pushSessionFn = unsafeBitCast(dlsym(handle, "seq_ch_push_agent_session"), to: PushSessionFn?.self)
        pushTurnFn = unsafeBitCast(dlsym(handle, "seq_ch_push_agent_turn"), to: PushTurnFn?.self)
        pushToolCallFn = unsafeBitCast(dlsym(handle, "seq_ch_push_agent_tool_call"), to: PushToolCallFn?.self)
        flushFn = unsafeBitCast(dlsym(handle, "seq_ch_flush"), to: FlushFn?.self)
    }

    public func pushAgentTurn(tsMs: UInt64, sessionID: String, turnIndex: UInt32,
                               agent: String, model: String,
                               inputTokens: UInt32, outputTokens: UInt32,
                               cachedTokens: UInt32, reasoningTokens: UInt32,
                               durMs: UInt32, costUsd: Double,
                               stopReason: String, isError: UInt8,
                               contextWindow: UInt32, contextUsedPct: Float) {
        guard let writer, let fn = pushTurnFn else { return }
        sessionID.withCString { sid in
        agent.withCString { a in
        model.withCString { m in
        stopReason.withCString { sr in
            fn(writer, tsMs, sid, turnIndex, a, m,
               inputTokens, outputTokens, cachedTokens, reasoningTokens,
               durMs, costUsd, sr, isError, contextWindow, contextUsedPct)
        }}}}
    }

    // pushAgentSession and pushAgentToolCall follow the same pattern
    // ...

    public func flush() {
        flushFn?(writer)
    }
}
```

### 3i. Entry Point

```swift
// Examples/AgentIngestDaemon/main.swift

import HiveAgentIngest
import Foundation

@main
struct Main {
    static func main() async throws {
        print("[agent-ingest] Starting JSONL ingest daemon")
        print("[agent-ingest] Watching ~/.claude/projects/ and ~/.codex/sessions/")

        let daemon = AgentIngestDaemon()
        try await daemon.run()
    }
}
```

**Test:**

```bash
cd ~/repos/christopherkarani/Hive/Sources/Hive
swift build --target AgentIngestDaemon

# Run it (safe mode: no history backfill)
swift run HiveAgentIngestDaemon

# Optional: backfill entire history (can be large)
swift run HiveAgentIngestDaemon --backfill

# In another terminal, verify data is flowing
clickhouse-client --query "SELECT agent, count() FROM agent.turns GROUP BY agent"
clickhouse-client --query "SELECT agent, tool_name, count() FROM agent.tool_calls GROUP BY agent, tool_name ORDER BY count() DESC LIMIT 10"
```

**Smoke test (end-to-end C ABI + ClickHouse):**

```bash
swift run HiveAgentIngestSmoke
clickhouse-client --query "SELECT * FROM agent.sessions WHERE session_id LIKE 'smoke-%' ORDER BY ts_ms DESC LIMIT 1"
```

---

## Step 4: Hive Agent — Context Refresh Graph

A Hive graph that periodically queries ClickHouse analytics and writes context summaries to Claude Code's CLAUDE.md memory files.

### 4a. New Target: HiveAgentContext

Location: `~/repos/christopherkarani/Hive/Sources/Hive/Sources/HiveAgentContext/`

Files:
- `AgentContextSchema.swift` — Hive schema with channels for agent state
- `ContextRefreshNodes.swift` — Hive nodes that query ClickHouse and update memory

### 4b. Schema

```swift
// AgentContextSchema.swift

import HiveCore

public enum AgentContextSchema: HiveSchema {
    public typealias InterruptPayload = String
    public typealias ResumePayload = String

    // Input: which project to refresh context for
    public static let activeProject = HiveChannelKey<Self, String>(HiveChannelID("ctx.activeProject"))

    // Output: context summary for injection
    public static let recentSessions = HiveChannelKey<Self, String>(HiveChannelID("ctx.recentSessions"))
    public static let recentErrors = HiveChannelKey<Self, String>(HiveChannelID("ctx.recentErrors"))
    public static let crossAgentSummary = HiveChannelKey<Self, String>(HiveChannelID("ctx.crossAgent"))

    // State
    public static let lastRefreshTs = HiveChannelKey<Self, UInt64>(HiveChannelID("ctx.lastRefreshTs"))

    public static var channelSpecs: [AnyHiveChannelSpec<Self>] {
        [
            AnyHiveChannelSpec(HiveChannelSpec(key: activeProject, scope: .global,
                reducer: .lastWriteWins(), updatePolicy: .single,
                initial: { "" }, persistence: .untracked)),
            AnyHiveChannelSpec(HiveChannelSpec(key: recentSessions, scope: .global,
                reducer: .lastWriteWins(), updatePolicy: .single,
                initial: { "" }, persistence: .untracked)),
            AnyHiveChannelSpec(HiveChannelSpec(key: recentErrors, scope: .global,
                reducer: .lastWriteWins(), updatePolicy: .single,
                initial: { "" }, persistence: .untracked)),
            AnyHiveChannelSpec(HiveChannelSpec(key: crossAgentSummary, scope: .global,
                reducer: .lastWriteWins(), updatePolicy: .single,
                initial: { "" }, persistence: .untracked)),
            AnyHiveChannelSpec(HiveChannelSpec(key: lastRefreshTs, scope: .global,
                reducer: .lastWriteWins(), updatePolicy: .single,
                initial: { 0 }, persistence: .untracked)),
        ]
    }
}
```

### 4c. ClickHouse Query Runner

For this phase, use `clickhouse-client` CLI subprocess (simple, no additional dependencies). Later can migrate to native protocol if needed.

```swift
// ClickHouseQuery.swift

import Foundation

enum ClickHouseQuery {
    static func run(_ sql: String) async throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/local/bin/clickhouse-client")
        process.arguments = ["--query", sql, "--format", "TSVWithNames"]

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice

        try process.run()
        process.waitUntilExit()

        return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    }
}
```

### 4d. Context Refresh Nodes

```swift
// ContextRefreshNodes.swift

import HiveCore
import HiveDSL
import Foundation

func buildContextRefreshGraph() throws -> CompiledHiveGraph<AgentContextSchema> {
    let workflow = Workflow<AgentContextSchema> {

        // Step 1: Query recent sessions for this project
        Node<AgentContextSchema>("query_sessions") { input in
            let project = try input.store.get(AgentContextSchema.activeProject)
            let result = try await ClickHouseQuery.run("""
                SELECT agent, model, session_id, turns,
                       total_input_tokens + total_output_tokens AS tokens,
                       round(total_cost_usd, 4) AS cost
                FROM agent.sessions
                WHERE project_path LIKE '%\(project)%'
                ORDER BY ts_ms DESC LIMIT 5
            """)
            return Effects {
                Set(AgentContextSchema.recentSessions, result)
                UseGraphEdges()
            }
        }.start()

        // Step 2: Query recent tool failures
        Node<AgentContextSchema>("query_errors") { input in
            let project = try input.store.get(AgentContextSchema.activeProject)
            let result = try await ClickHouseQuery.run("""
                SELECT t.agent, t.tool_name, t.input_summary, count() AS failures
                FROM agent.tool_calls t
                JOIN agent.sessions s ON t.session_id = s.session_id
                WHERE s.project_path LIKE '%\(project)%'
                  AND t.ok = 0
                  AND t.ts_ms > toUnixTimestamp(now() - INTERVAL 2 HOUR) * 1000
                GROUP BY t.agent, t.tool_name, t.input_summary
                ORDER BY failures DESC LIMIT 5
            """)
            return Effects {
                Set(AgentContextSchema.recentErrors, result)
                UseGraphEdges()
            }
        }

        // Step 3: Query cross-agent activity (what did the OTHER agent do last?)
        Node<AgentContextSchema>("query_cross_agent") { input in
            let project = try input.store.get(AgentContextSchema.activeProject)
            let result = try await ClickHouseQuery.run("""
                SELECT agent, model, turns,
                       total_input_tokens + total_output_tokens AS tokens,
                       round(total_cost_usd, 4) AS cost,
                       round((now() - toDateTime(ts_ms / 1000)) / 60) AS minutes_ago
                FROM agent.sessions
                WHERE project_path LIKE '%\(project)%'
                ORDER BY ts_ms DESC LIMIT 3
            """)
            return Effects {
                Set(AgentContextSchema.crossAgentSummary, result)
                UseGraphEdges()
            }
        }

        // Step 4: Write context to memory file
        Node<AgentContextSchema>("write_memory") { input in
            let project = try input.store.get(AgentContextSchema.activeProject)
            let sessions = try input.store.get(AgentContextSchema.recentSessions)
            let errors = try input.store.get(AgentContextSchema.recentErrors)
            let crossAgent = try input.store.get(AgentContextSchema.crossAgentSummary)

            let memory = """
            # Agent Session Context (auto-generated)

            ## Recent Sessions
            \(sessions)

            ## Recent Failures
            \(errors.isEmpty ? "None" : errors)

            ## Cross-Agent Activity
            \(crossAgent)

            _Updated: \(ISO8601DateFormatter().string(from: Date()))_
            """

            // Write to Claude Code memory
            let claudeMemDir = "\(NSHomeDirectory())/.claude/projects"
            // Find the right project hash directory
            if let projectHash = findClaudeProjectHash(project: project, in: claudeMemDir) {
                let memoryDir = "\(claudeMemDir)/\(projectHash)/memory"
                try FileManager.default.createDirectory(atPath: memoryDir,
                    withIntermediateDirectories: true)
                try memory.write(toFile: "\(memoryDir)/agent-context.md",
                    atomically: true, encoding: .utf8)
            }

            return Effects {
                Set(AgentContextSchema.lastRefreshTs,
                    UInt64(Date().timeIntervalSince1970 * 1000))
            }
        }

        // Edges: linear pipeline
        Edge<AgentContextSchema>("query_sessions", to: "query_errors")
        Edge<AgentContextSchema>("query_errors", to: "query_cross_agent")
        Edge<AgentContextSchema>("query_cross_agent", to: "write_memory")
    }

    return try workflow.compile()
}
```

**Test:**

```bash
swift build --target HiveAgentContextRefresh

# Run the context refresh (one-shot)
swift run HiveAgentContextRefresh --project /Users/nikiv/code/seq --hours 24

# Check the generated memory file
cat ~/.claude/projects/*/memory/agent-context.md
```

---

## Step 5: Dashboard Queries

These are ready-to-run SQL queries. No code changes needed — just apply the schema from Step 1.

**Daily cost report:**

```sql
SELECT agent, model,
    sum(input_tokens) AS input_tok,
    sum(output_tokens) AS output_tok,
    sum(cached_tokens) AS cached_tok,
    round(sum(cached_tokens) * 100.0 / nullIf(sum(input_tokens), 0), 1) AS cache_pct,
    round(sum(cost_usd), 4) AS cost,
    sum(calls) AS api_calls
FROM agent.cost_hourly
WHERE hour >= toStartOfDay(now())
GROUP BY agent, model
ORDER BY cost DESC;
```

**Tool success rate (last 7 days):**

```sql
SELECT agent, tool_name,
    sum(calls) AS total,
    sum(failures) AS fails,
    round((sum(calls) - sum(failures)) * 100.0 / sum(calls), 1) AS success_pct
FROM agent.tool_success
WHERE day >= today() - 7
GROUP BY agent, tool_name
ORDER BY total DESC;
```

**Coding time vs AI time correlation:**

```sql
SELECT
    toStartOfHour(toDateTime(c.ts_ms / 1000)) AS hour,
    round(sumIf(c.dur_ms, c.app IN ('Xcode', 'Cursor', 'Zed', 'Terminal')) / 60000, 1) AS coding_min,
    (SELECT count() FROM agent.turns t
     WHERE toStartOfHour(toDateTime(t.ts_ms / 1000)) = hour) AS ai_calls,
    (SELECT sum(output_tokens) FROM agent.turns t
     WHERE toStartOfHour(toDateTime(t.ts_ms / 1000)) = hour) AS ai_tokens
FROM seq.context c
WHERE c.ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour ORDER BY hour;
```

---

## Implementation Order (dependency chain)

```
Step 1: Schema ─────────────┐
                             ├─→ Step 3: Ingest Daemon (depends on 1 + 2)
Step 2: C ABI ──────────────┘         │
                                      ├─→ Step 4: Context Refresh (depends on 3 having data)
                                      │
                                      └─→ Step 5: Dashboard Queries (depends on 3 having data)
```

**Estimated effort per step:**

| Step | Files Changed | Files Created | Complexity |
|------|--------------|---------------|------------|
| 1. Schema | 1 | 0 | Low — append SQL to existing file |
| 2. C ABI | 4 | 0 | Medium — follow existing pattern exactly |
| 3. Ingest | 1 (Package.swift) | 6 | High — FSEvents, JSONL parsing, dlopen bridge |
| 4. Context | 1 (Package.swift) | 4 | Medium — Hive graph + subprocess query |
| 5. Queries | 0 | 0 | Low — just SQL |

**Total: 7 files modified, 10 files created.**

## Future Extensions (not in this plan)

- **Codex `app-server` bridge:** Hive tool that spawns `codex app-server` for `review/start` integration
- **Cross-agent session handoff:** Automatic context injection when switching between Claude and Codex
- **Real-time dashboard:** TUI or web view reading from ClickHouse
- **Cost alerting:** Hive graph that monitors `agent.cost_hourly` and interrupts when budget exceeded
- **MLX routing based on spend:** Route to local MLX model when cloud cost is high
