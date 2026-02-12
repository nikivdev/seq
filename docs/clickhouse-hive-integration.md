# ClickHouse + Hive Integration

Full observability for always-on agentic workloads. Every LLM call, tool execution, context switch, and agent decision lands in ClickHouse within 100ms.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                   ClickHouse (local)                     │
│  seq.mem_events   seq.context    hive.supersteps         │
│  seq.trace_events hive.model_invocations hive.tool_calls │
│  ──────── materialized views (auto-updating) ─────────── │
│  seq.app_minutes_daily   hive.cost_per_hour              │
│  hive.error_hotspots     hive.tool_error_hotspots        │
└──────▲────────────────────────▲───────────────▲──────────┘
       │                        │               │
  native proto             native proto    native proto
  (libseqch.dylib)        (libseqch.dylib)
       │                        │               │
┌──────┴──────┐      ┌─────────┴──────┐   ┌────┴────────┐
│   seqd      │◄────►│    Hive        │◄──│ MLX local   │
│  C++ daemon │      │ Swift graphs   │   │ models      │
│  context    │      │ agent loops    │   │ (mlx-swift) │
│  OCR/frames │      │ checkpoints   │   └─────────────┘
│  AFK detect │      │ tool calling  │
│  macros     │      │ streaming     │
└─────────────┘      └───────────────┘
```

### Data flow

1. **seqd** polls window context, AFK status, captures screen frames
2. Events flow to ClickHouse via `libseqch.dylib` (native binary protocol, async batching)
3. **Hive** runs agent workflows as deterministic graphs (BSP supersteps)
4. Hive's event stream is consumed by `HiveClickHouseSink` (Swift actor, dlopen's `libseqch.dylib`)
5. **MLX** provides local model inference (~30-60 tok/s) for fast routing/classification
6. **SeqContextPoller** feeds Mac context into Hive channels via Unix socket to seqd

## Components

### libseqch.dylib (C ABI)

Shared library wrapping clickhouse-cpp with async batch writing. Both seqd (C++) and Hive (Swift via dlopen) use it.

**C ABI functions:**

| Function | What it pushes |
|----------|---------------|
| `seq_ch_push_mem_event` | Memory engine events (app context, metrics) |
| `seq_ch_push_trace_event` | Structured trace/log events |
| `seq_ch_push_context` | Window context segments (app, title, URL, AFK) |
| `seq_ch_push_superstep` | Hive BSP superstep lifecycle |
| `seq_ch_push_model_invocation` | LLM calls (MLX or cloud, tokens, latency) |
| `seq_ch_push_tool_call` | Tool executions (input, output, duration) |

All push functions are lock-free on the write side (mutex-protected queue + background flush thread).

### HiveClickHouse (Swift target)

`HiveClickHouseSink` actor that consumes `AsyncThrowingStream<HiveEvent>` and pushes events to ClickHouse.

```swift
let sink = HiveClickHouseSink(graphName: "my_agent")
let handle = await runtime.run(threadID: threadID, input: (), options: opts)

// Background: consume all events → ClickHouse
Task { await sink.consume(handle.events, threadID: threadID.rawValue) }
```

### HiveMLX (Swift target)

`MLXModelClient` implementing `HiveModelClient` for on-device inference via mlx-swift.

```swift
let mlx = try await MLXModelClient(
    modelPath: "mlx-community/Qwen2.5-7B-Instruct-4bit",
    maxTokens: 2048
)
// Use as any HiveModelClient
let response = try await mlx.complete(request)
```

`MLXModelRouter` routes between local and cloud based on `HiveInferenceHints`:

```swift
let router = MLXModelRouter(mlx: mlxClient, cloud: claudeClient)
let client = router.route(request, hints: HiveInferenceHints(
    latencyTier: .interactive,
    privacyRequired: true,
    tokenBudget: nil,
    networkState: .online
))
// → routes to MLX (privacy required)
```

### HiveSeqBridge (Swift target)

`SeqContextPoller` connects to seqd's Unix socket and provides context snapshots.

`SeqContextSchema` defines Hive channels for Mac context (active app, window title, URL, AFK, idle time, OCR).

```swift
let poller = SeqContextPoller()
for await ctx in await poller.stream(intervalSeconds: 3.0) {
    print("\(ctx.activeApp): \(ctx.windowTitle) (AFK: \(ctx.isAFK))")
}
```

## Setup

### 1. Start ClickHouse

```bash
cd ~/code/seq
./tools/clickhouse/local_server.sh start
```

### 2. Apply schema

```bash
./tools/clickhouse/seqmem_setup.sh
```

This creates:

**`seq` database:**
- `mem_events` — memory engine events
- `trace_events` — structured trace logs
- `window_context` + `mv_window_context` — parsed window context (from mem_events)
- `context` — direct context segments (from seqd native protocol)
- `app_minutes_daily` + `mv_app_minutes` — auto-aggregated app usage

**`hive` database:**
- `supersteps` — every BSP superstep
- `model_invocations` — every LLM call (local + cloud)
- `tool_calls` — every tool execution
- `cost_per_hour` + `mv_cost_per_hour` — auto-aggregated token cost
- `error_hotspots` + `mv_error_hotspots` — auto-tracked failing nodes
- `tool_error_hotspots` + `mv_tool_error_hotspots` — auto-tracked failing tools

### 3. Build seq

```bash
cd ~/code/seq

# Force rebuild of libseqch.dylib if C ABI changed
rm -f cli/cpp/out/build/ch/libseqch.dylib

sh cli/cpp/run.sh build
```

### 4. Build Hive

Build each target individually to avoid transitive `_NumericsShims` issues with the MLX dependency:

```bash
cd ~/repos/christopherkarani/Hive/Sources/Hive

# Core targets (fast)
swift build --target HiveClickHouse
swift build --target HiveSeqBridge
swift build --target HiveAlwaysOnAgent

# MLX target (slower, pulls mlx-swift)
swift build --target HiveMLX
```

**Dependency notes:** `swift-transformers` is pinned to `0.1.20` to avoid an ambiguous `dictionary` API introduced in `0.1.21` that breaks `mlx-swift-examples 1.16.0`.

### 5. Run the always-on agent

```bash
cd ~/repos/christopherkarani/Hive/Sources/Hive
swift run HiveAlwaysOnAgent
```

## ClickHouse Schema

### seq.context

Direct window context segments written by seqd.

```sql
CREATE TABLE seq.context (
    ts_ms        UInt64,          -- epoch milliseconds
    dur_ms       UInt64,          -- segment duration
    app          LowCardinality(String),
    bundle_id    LowCardinality(String),
    window_title String,
    url          String DEFAULT '',
    afk          UInt8 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (app, ts_ms)
TTL toDateTime(ts_ms / 1000) + INTERVAL 90 DAY;
```

### hive.supersteps

Every BSP superstep in every graph run.

```sql
CREATE TABLE hive.supersteps (
    ts_ms           UInt64,
    thread_id       String,
    graph_name      LowCardinality(String),
    graph_version   UInt32,
    step_index      UInt32,
    frontier_count  UInt32,       -- tasks in this step
    writes          UInt32,       -- channel writes committed
    dur_us          UInt64,       -- step duration
    status          Enum8('started'=0, 'finished'=1, 'interrupted'=2, 'error'=3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (graph_name, thread_id, step_index);
```

### hive.model_invocations

Every LLM call — local MLX or cloud.

```sql
CREATE TABLE hive.model_invocations (
    ts_ms           UInt64,
    thread_id       String,
    node_id         LowCardinality(String),
    graph_name      LowCardinality(String),
    provider        LowCardinality(String),  -- 'mlx', 'claude', 'openai'
    model           LowCardinality(String),
    input_tokens    UInt32,
    output_tokens   UInt32,
    dur_us          UInt64,
    ttft_us         UInt64,                  -- time to first token
    tool_calls      UInt16,
    ok              UInt8,
    error_msg       String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (provider, model, ts_ms);
```

### hive.tool_calls

Every tool execution.

```sql
CREATE TABLE hive.tool_calls (
    ts_ms       UInt64,
    thread_id   String,
    node_id     LowCardinality(String),
    tool_name   LowCardinality(String),
    input_json  String,
    output_json String,
    dur_us      UInt64,
    ok          UInt8
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (tool_name, ts_ms);
```

## Materialized Views

These update automatically on every insert — zero query cost, no cron jobs.

### Token cost per model per hour

```sql
-- Auto-populated table
SELECT model, hour, input_tokens, output_tokens, calls
FROM hive.cost_per_hour
ORDER BY hour DESC, calls DESC;
```

### App usage per day

```sql
SELECT app, day, minutes
FROM seq.app_minutes_daily
ORDER BY day DESC, minutes DESC;
```

### Error hotspots

```sql
-- Which graph nodes fail most?
SELECT graph_name, node_id, errors
FROM hive.error_hotspots
ORDER BY errors DESC;

-- Which tools fail most?
SELECT tool_name, errors
FROM hive.tool_error_hotspots
ORDER BY errors DESC;
```

## Queries

### Agent performance: p50/p99 latency per node

```sql
SELECT
    node_id,
    count() AS calls,
    quantile(0.5)(dur_us) / 1000 AS p50_ms,
    quantile(0.99)(dur_us) / 1000 AS p99_ms,
    avg(input_tokens + output_tokens) AS avg_tokens
FROM hive.model_invocations
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 1 HOUR) * 1000
GROUP BY node_id
ORDER BY calls DESC;
```

### MLX vs cloud usage today

```sql
SELECT
    provider,
    count() AS calls,
    sum(input_tokens) AS total_in,
    sum(output_tokens) AS total_out,
    avg(dur_us) / 1000 AS avg_ms
FROM hive.model_invocations
WHERE ts_ms > toUnixTimestamp(toStartOfDay(now())) * 1000
GROUP BY provider;
```

### Agent activity timeline (last hour)

```sql
SELECT
    toDateTime(ts_ms / 1000) AS time,
    graph_name,
    step_index,
    frontier_count,
    dur_us / 1000 AS dur_ms,
    status
FROM hive.supersteps
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 1 HOUR) * 1000
ORDER BY ts_ms;
```

### Tool call patterns

```sql
SELECT
    tool_name,
    count() AS calls,
    countIf(ok = 0) AS failures,
    avg(dur_us) / 1000 AS avg_ms
FROM hive.tool_calls
WHERE ts_ms > toUnixTimestamp(toStartOfDay(now())) * 1000
GROUP BY tool_name
ORDER BY calls DESC;
```

### Context switches per hour (how often you switch apps)

```sql
SELECT
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    count() AS switches,
    uniqExact(app) AS unique_apps
FROM seq.context
WHERE ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour
ORDER BY hour;
```

### Correlation: coding time vs agent calls

```sql
SELECT
    toStartOfHour(toDateTime(c.ts_ms / 1000)) AS hour,
    sumIf(c.dur_ms, c.bundle_id IN ('com.apple.dt.Xcode', 'dev.zed.Zed')) / 60000 AS coding_min,
    (SELECT count() FROM hive.model_invocations m
     WHERE m.ts_ms BETWEEN c.ts_ms AND c.ts_ms + 3600000) AS agent_calls
FROM seq.context c
WHERE c.ts_ms > toUnixTimestamp(now() - INTERVAL 24 HOUR) * 1000
GROUP BY hour
ORDER BY hour;
```

## Always-On Agent Workflow

The `HiveAlwaysOnAgent` example demonstrates the full integration:

```
context_poll → context_router → coding_assist    ─┐
                              → research_assist   ├→ (loop back to context_poll)
                              → general_observe   ─┘
                              → idle_checkpoint   → (interrupt, resume on return)
```

### Flow

1. `context_poll` — increments loop counter, proceeds to router
2. `context_router` — reads `seq.activeApp` and `seq.isAFK` channels:
   - Xcode/Terminal/Cursor → `coding_assist`
   - Arc/Safari/Chrome → `research_assist`
   - AFK → `idle_checkpoint` (interrupts, checkpoints state)
   - Other → `general_observe`
3. Each handler writes to `agent.lastAction` and `agent.suggestion`, then loops back
4. On AFK interrupt, the graph checkpoints to Wax and pauses
5. Resume with `runtime.resume(threadID:interruptID:payload:)` when back

### Events traced to ClickHouse

Every superstep, every model call (if you add MLX nodes), every tool call, and every context switch — all land in ClickHouse with sub-100ms latency via the async batch writer.

## Resource Usage (measured on M-series Mac)

| Component | Idle RAM | Idle CPU | Startup |
|-----------|----------|----------|---------|
| ClickHouse server | 82 MB | 0% | ~1s |
| libseqch.dylib | 0 (in-process) | 0 | instant |
| MLX 8B Q4 model | ~4.5 GB | 0% | ~2s |
| seqd daemon | ~15 MB | <0.5% | instant |
| Hive runtime | ~5 MB | 0% | instant |

Total idle overhead: ~100 MB (without MLX model) or ~4.6 GB (with model loaded).

## File Locations

| File | Purpose |
|------|---------|
| `tools/clickhouse/seqmem.sql` | Full schema (seq + hive databases) |
| `tools/clickhouse/seqmem_setup.sh` | Apply schema to running ClickHouse |
| `tools/clickhouse/local_server.sh` | Start/stop local ClickHouse |
| `cli/cpp/src/clickhouse.h` | Row types + Client/AsyncWriter |
| `cli/cpp/src/clickhouse.cpp` | Insert methods for all 6 row types |
| `cli/cpp/src/clickhouse_bridge.h` | C ABI function declarations |
| `cli/cpp/src/clickhouse_bridge.cpp` | C ABI implementations |
| `cli/cpp/deps/CMakeLists.txt` | CMake build for libseqch.dylib |

**Hive targets** (in `~/repos/christopherkarani/Hive/Sources/Hive/Sources/`):

| Target | Files | Purpose |
|--------|-------|---------|
| `HiveClickHouse/` | `HiveClickHouseSink.swift` | Event stream → ClickHouse |
| `HiveMLX/` | `MLXModelClient.swift` | On-device LLM via mlx-swift |
| `HiveSeqBridge/` | `SeqContextPoller.swift`, `SeqContextSchema.swift` | Mac context → Hive channels |
| `Examples/AlwaysOnAgent/` | `main.swift` | Runnable always-on agent |
