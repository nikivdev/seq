# Use Cases

Practical workflows combining seq context, Hive agent graphs, local MLX inference, and ClickHouse observability.

## 1. Context-Aware Coding Assistant

Watches what you're editing, suggests improvements using local MLX model. No data leaves your machine.

### Workflow

```
context_poll → is_coding? ─yes→ extract_file_info → mlx_suggest → log_suggestion
                          └no─→ (skip, loop back)
```

### Hive Graph

```swift
import HiveCore
import HiveDSL
import HiveSeqBridge

enum CodingSchema: HiveSchema {
    typealias InterruptPayload = String
    typealias ResumePayload = String

    static let activeApp = SeqContextChannels.activeApp
    static let windowTitle = SeqContextChannels.windowTitle
    static let isAFK = SeqContextChannels.isAFK
    static let suggestion = HiveChannelKey<Self, String>(HiveChannelID("agent.suggestion"))
    static let fileContext = HiveChannelKey<Self, String>(HiveChannelID("agent.fileContext"))

    static var channelSpecs: [AnyHiveChannelSpec<Self>] {
        [
            AnyHiveChannelSpec(HiveChannelSpec(key: activeApp, scope: .global,
                reducer: .lastWriteWins(), initial: { "" }, persistence: .untracked)),
            AnyHiveChannelSpec(HiveChannelSpec(key: windowTitle, scope: .global,
                reducer: .lastWriteWins(), initial: { "" }, persistence: .untracked)),
            AnyHiveChannelSpec(HiveChannelSpec(key: isAFK, scope: .global,
                reducer: .lastWriteWins(), initial: { false }, persistence: .untracked)),
            AnyHiveChannelSpec(HiveChannelSpec(key: suggestion, scope: .global,
                reducer: .lastWriteWins(), initial: { "" }, persistence: .checkpointed)),
            AnyHiveChannelSpec(HiveChannelSpec(key: fileContext, scope: .global,
                reducer: .lastWriteWins(), initial: { "" }, persistence: .untracked)),
        ]
    }
}

let workflow = Workflow<CodingSchema> {
    Node<CodingSchema>("context_poll") { input in
        let app = try input.store.get(CodingSchema.activeApp)
        let codingApps = ["Xcode", "Cursor", "Visual Studio Code", "Zed", "Terminal", "iTerm2"]
        if codingApps.contains(app) {
            return Effects { GoTo("extract_file_info") }
        }
        return Effects { GoTo("context_poll") }  // loop, wait for coding context
    }.start()

    Node<CodingSchema>("extract_file_info") { input in
        let title = try input.store.get(CodingSchema.windowTitle)
        // Extract filename from window title (e.g. "main.swift — MyProject")
        let file = title.components(separatedBy: " — ").first ?? title
        return Effects {
            Set(CodingSchema.fileContext, "Editing: \(file)")
            GoTo("mlx_suggest")
        }
    }

    // ModelTurn calls local MLX model for code suggestion
    ModelTurn<CodingSchema>("mlx_suggest", model: "mlx", messages: [
        HiveChatMessage(id: "sys", role: .system,
            content: "You are a coding assistant. Given the file context, suggest one improvement.")
    ])
    .writes(to: CodingSchema.suggestion)

    Node<CodingSchema>("log_suggestion") { input in
        let suggestion = try input.store.get(CodingSchema.suggestion)
        print("[coding-assist] \(suggestion)")
        return Effects { GoTo("context_poll") }
    }

    Edge<CodingSchema>("mlx_suggest", to: "log_suggestion")
}
```

### ClickHouse Queries

```sql
-- What files got the most suggestions today?
SELECT
    extractTextBetween(graph_name, 'Editing: ', '') AS file,
    count() AS suggestions,
    avg(dur_us) / 1000 AS avg_latency_ms
FROM hive.model_invocations
WHERE ts_ms > toUnixTimestamp(toStartOfDay(now())) * 1000
  AND provider = 'mlx'
GROUP BY file
ORDER BY suggestions DESC;

-- Suggestion latency by hour (is MLX getting slower under load?)
SELECT
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    count() AS calls,
    quantile(0.5)(dur_us) / 1000 AS p50_ms,
    quantile(0.99)(dur_us) / 1000 AS p99_ms
FROM hive.model_invocations
WHERE provider = 'mlx'
  AND ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour
ORDER BY hour;
```

---

## 2. Research Tracker

Tracks URLs you visit in the browser, classifies topics, builds a searchable session log.

### Workflow

```
context_poll → is_browser? ─yes→ url_changed? ─yes→ classify_url → log_research
                            └no─→ (loop)       └no─→ (loop)
```

### Key Logic

```swift
Node<ResearchSchema>("url_tracker") { input in
    let app = try input.store.get(ResearchSchema.activeApp)
    let url = try input.store.get(ResearchSchema.activeURL)
    let lastURL = try input.store.get(ResearchSchema.lastURL)

    let browsers = ["Arc", "Safari", "Google Chrome", "Firefox"]
    guard browsers.contains(app), !url.isEmpty, url != lastURL else {
        return Effects { GoTo("context_poll") }
    }

    return Effects {
        Set(ResearchSchema.lastURL, url)
        GoTo("classify_url")
    }
}

// MLX classifies the URL domain/path into a topic
ModelTurn<ResearchSchema>("classify_url", model: "mlx", messages: { store in
    let url = try store.get(ResearchSchema.activeURL)
    return [
        HiveChatMessage(id: "sys", role: .system,
            content: "Classify this URL into one topic (1-3 words): \(url)")
    ]
})
.writes(to: ResearchSchema.currentTopic)
```

### ClickHouse Queries

```sql
-- Research sessions: what domains do you visit most?
SELECT
    domain(url) AS site,
    count() AS visits,
    sum(dur_ms) / 60000 AS minutes
FROM seq.context
WHERE app IN ('Arc', 'Safari', 'Google Chrome')
  AND url != ''
  AND ts_ms > toUnixTimestamp(toStartOfDay(now())) * 1000
GROUP BY site
ORDER BY minutes DESC
LIMIT 20;

-- Topic classification accuracy: how many MLX calls per research session?
SELECT
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    count() AS classifications,
    avg(output_tokens) AS avg_tokens
FROM hive.model_invocations
WHERE graph_name = 'research_tracker'
  AND ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour
ORDER BY hour;
```

---

## 3. Focus Timer with Automatic Pausing

Tracks deep work sessions. Pauses when you go AFK or switch to non-work apps. Resumes automatically when you return.

### Workflow

```
start_session → monitor_focus ──focused──→ (loop, increment timer)
                               ├─distracted─→ warn_distraction → monitor_focus
                               └─AFK────────→ checkpoint_pause ──(resume)──→ monitor_focus
```

### Key Logic

```swift
Node<FocusSchema>("monitor_focus") { input in
    let app = try input.store.get(FocusSchema.activeApp)
    let afk = try input.store.get(FocusSchema.isAFK)
    let focusApps = try input.store.get(FocusSchema.focusApps)
    let elapsed = try input.store.get(FocusSchema.elapsedMinutes)

    if afk {
        return Effects {
            Set(FocusSchema.sessionState, "paused")
            Interrupt("AFK detected after \(elapsed) minutes of focus")
        }
    }

    if !focusApps.contains(app) {
        return Effects {
            Set(FocusSchema.distractionCount, try input.store.get(FocusSchema.distractionCount) + 1)
            Set(FocusSchema.lastDistraction, app)
            GoTo("warn_distraction")
        }
    }

    return Effects {
        Set(FocusSchema.elapsedMinutes, elapsed + 1)
        GoTo("monitor_focus")
    }
}
```

### Resume After AFK

```swift
// When user returns from AFK, resume the graph
let resumed = await runtime.resume(
    threadID: threadID,
    interruptID: interruption.interrupt.id,
    payload: "back",
    options: HiveRunOptions(checkpointPolicy: .onInterrupt)
)
```

### ClickHouse Queries

```sql
-- Daily focus time vs distraction time
SELECT
    toDate(toDateTime(ts_ms / 1000)) AS day,
    sumIf(dur_ms, app IN ('Xcode', 'Cursor', 'Terminal')) / 60000 AS focus_min,
    sumIf(dur_ms, app IN ('Slack', 'Discord', 'Messages')) / 60000 AS distraction_min,
    countIf(afk = 1) AS afk_events
FROM seq.context
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 7 DAY) * 1000
GROUP BY day
ORDER BY day;

-- How long are your AFK breaks?
SELECT
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    count() AS breaks,
    avg(dur_ms) / 60000 AS avg_break_min,
    max(dur_ms) / 60000 AS longest_break_min
FROM seq.context
WHERE afk = 1
  AND ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour
ORDER BY hour;

-- Focus sessions interrupted by Hive (checkpoint events)
SELECT
    toDateTime(ts_ms / 1000) AS time,
    graph_name,
    step_index,
    status
FROM hive.supersteps
WHERE status = 'interrupted'
  AND ts_ms > toUnixTimestamp(toStartOfDay(now())) * 1000
ORDER BY ts_ms;
```

---

## 4. MLX vs Cloud Model A/B Testing

Routes identical prompts to both local MLX and cloud, compares quality and latency.

### Setup

```swift
let mlx = try await MLXModelClient(
    modelPath: "mlx-community/Qwen2.5-7B-Instruct-4bit",
    maxTokens: 512
)
let cloud = ClaudeClient(apiKey: ProcessInfo.processInfo.environment["ANTHROPIC_API_KEY"]!)
let router = MLXModelRouter(mlx: mlx, cloud: cloud)

// Route based on hints
let client = router.route(request, hints: HiveInferenceHints(
    latencyTier: .interactive,
    privacyRequired: false,
    tokenBudget: nil,
    networkState: .online
))
```

### ClickHouse Queries

```sql
-- MLX vs cloud: latency and token comparison
SELECT
    provider,
    count() AS calls,
    avg(dur_us) / 1000 AS avg_ms,
    quantile(0.5)(dur_us) / 1000 AS p50_ms,
    quantile(0.99)(dur_us) / 1000 AS p99_ms,
    avg(input_tokens) AS avg_input,
    avg(output_tokens) AS avg_output,
    countIf(ok = 0) AS errors
FROM hive.model_invocations
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY provider;

-- Token throughput: tokens per second by provider
SELECT
    provider,
    model,
    avg(output_tokens * 1000000.0 / dur_us) AS tokens_per_sec,
    count() AS sample_size
FROM hive.model_invocations
WHERE dur_us > 0
  AND ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY provider, model
ORDER BY tokens_per_sec DESC;

-- Cost accumulation per hour (assumes cloud tokens have cost)
SELECT
    model,
    hour,
    input_tokens,
    output_tokens,
    calls
FROM hive.cost_per_hour
WHERE hour > toStartOfHour(now() - INTERVAL 24 HOUR)
ORDER BY hour DESC, calls DESC;
```

---

## 5. App Usage Analytics Dashboard

No agent needed. Pure ClickHouse queries over seq context data collected by seqd.

### Daily Report

```sql
-- Top apps by usage today
SELECT
    app,
    round(sum(dur_ms) / 60000, 1) AS minutes,
    count() AS window_switches,
    uniqExact(window_title) AS unique_windows
FROM seq.context
WHERE ts_ms > toUnixTimestamp(toStartOfDay(now())) * 1000
  AND afk = 0
GROUP BY app
ORDER BY minutes DESC;
```

### Context Switching Analysis

```sql
-- How often do you switch apps per hour?
SELECT
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    count() AS switches,
    uniqExact(app) AS unique_apps,
    uniqExact(bundle_id) AS unique_bundles
FROM seq.context
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
  AND afk = 0
GROUP BY hour
ORDER BY hour;

-- Most common app transitions (what do you switch between?)
SELECT
    app AS from_app,
    neighbor(app, 1) AS to_app,
    count() AS transitions
FROM seq.context
WHERE ts_ms > toUnixTimestamp(toStartOfDay(now())) * 1000
  AND afk = 0
GROUP BY from_app, to_app
HAVING from_app != to_app
ORDER BY transitions DESC
LIMIT 20;
```

### Weekly Trends

```sql
-- Weekly app usage trend
SELECT
    toMonday(toDate(toDateTime(ts_ms / 1000))) AS week,
    app,
    round(sum(dur_ms) / 3600000, 1) AS hours
FROM seq.context
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 30 DAY) * 1000
  AND afk = 0
GROUP BY week, app
ORDER BY week, hours DESC;
```

---

## 6. Error Hotspot Debugger

Automatically identifies which graph nodes and tools fail most, and correlates failures with context.

### ClickHouse Queries

```sql
-- Which nodes fail most?
SELECT
    graph_name,
    node_id,
    errors
FROM hive.error_hotspots
ORDER BY errors DESC
LIMIT 10;

-- Which tools fail most?
SELECT
    tool_name,
    errors
FROM hive.tool_error_hotspots
ORDER BY errors DESC;

-- Error timeline: when do failures cluster?
SELECT
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    count() AS total_calls,
    countIf(ok = 0) AS failures,
    round(countIf(ok = 0) * 100.0 / count(), 1) AS failure_pct
FROM hive.model_invocations
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour
ORDER BY hour;

-- Failed model calls with error messages
SELECT
    toDateTime(ts_ms / 1000) AS time,
    graph_name,
    node_id,
    provider,
    model,
    error_msg
FROM hive.model_invocations
WHERE ok = 0
  AND ts_ms > toUnixTimestamp(toStartOfDay(now())) * 1000
ORDER BY ts_ms DESC;

-- Correlate tool failures with active app (what were you doing when it broke?)
SELECT
    t.tool_name,
    c.app AS active_app_at_failure,
    count() AS failures
FROM hive.tool_calls t
ASOF JOIN seq.context c ON t.ts_ms >= c.ts_ms
WHERE t.ok = 0
  AND t.ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY t.tool_name, active_app_at_failure
ORDER BY failures DESC;
```

---

## 7. Coding + Agent Correlation

Understand how agent activity relates to your coding patterns.

### ClickHouse Queries

```sql
-- Coding time vs agent calls per hour
SELECT
    toStartOfHour(toDateTime(c.ts_ms / 1000)) AS hour,
    round(sumIf(c.dur_ms, c.bundle_id IN (
        'com.apple.dt.Xcode', 'dev.zed.Zed', 'com.todesktop.230313mzl4w4u92'
    )) / 60000, 1) AS coding_min,
    (SELECT count() FROM hive.model_invocations m
     WHERE toStartOfHour(toDateTime(m.ts_ms / 1000)) = hour) AS agent_calls,
    (SELECT count() FROM hive.tool_calls t
     WHERE toStartOfHour(toDateTime(t.ts_ms / 1000)) = hour) AS tool_calls
FROM seq.context c
WHERE c.ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour
ORDER BY hour;

-- When agent helps most: ratio of suggestions to coding time
SELECT
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    count() AS suggestions,
    avg(output_tokens) AS avg_tokens,
    round(sum(dur_us) / 1000000, 1) AS total_inference_sec
FROM hive.model_invocations
WHERE provider = 'mlx'
  AND ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour
HAVING suggestions > 0
ORDER BY hour;
```

---

## Testing

### Prerequisites

```bash
# 1. ClickHouse running
cd ~/code/seq && ./tools/clickhouse/local_server.sh start

# 2. Schema applied
./tools/clickhouse/seqmem_setup.sh

# 3. seqd running (for context data)
# seqd should already be running as a daemon

# 4. Hive built
cd ~/repos/christopherkarani/Hive/Sources/Hive
swift build --target HiveAlwaysOnAgent
```

### Quick Smoke Test

```bash
# Run the always-on agent
cd ~/repos/christopherkarani/Hive/Sources/Hive
swift run HiveAlwaysOnAgent

# In another terminal, check ClickHouse for data
~/code/seq/tools/clickhouse/clickhouse-local-client.sh \
    --query "SELECT count() FROM hive.supersteps"

~/code/seq/tools/clickhouse/clickhouse-local-client.sh \
    --query "SELECT * FROM hive.supersteps ORDER BY ts_ms DESC LIMIT 5 FORMAT Vertical"
```

### Verify Context Data

```bash
# Check seq context is flowing
~/code/seq/tools/clickhouse/clickhouse-local-client.sh \
    --query "SELECT app, window_title, afk FROM seq.context ORDER BY ts_ms DESC LIMIT 5"

# Check materialized view aggregates
~/code/seq/tools/clickhouse/clickhouse-local-client.sh \
    --query "SELECT app, minutes FROM seq.app_minutes_daily WHERE day = today() ORDER BY minutes DESC"
```

### Verify Model Invocations

```bash
# After running an agent with MLX
~/code/seq/tools/clickhouse/clickhouse-local-client.sh \
    --query "SELECT provider, model, dur_us/1000 AS ms, ok FROM hive.model_invocations ORDER BY ts_ms DESC LIMIT 5"
```

---

## 8. On-Demand Dependency Skills (Scraper-Powered)

Generate fresh dependency skills from live docs using the always-on scraper daemon/API.

### Workflow

```
teach-dep/teach-auto → enqueue scrape jobs → poll completion → compile skill files
```

### Commands

```bash
# Check scraper health
f scrape-health

# Generate one dependency skill
f teach-dep react

# Auto-generate top dependencies from current repo manifests
f teach-auto --top 2
```

### Output

- `.ai/skills/generated/<dep>/SKILL.md`
- `.ai/skills/generated/<dep>/sources.json`

### Performance

- Uses scraper queue endpoints (`/jobs`) instead of blocking single requests.
- Adaptive polling and persistent cache (`.ai/internal/teach-cache.json`, default 24h TTL).
- `--force` for cache bypass when you need the latest docs.
