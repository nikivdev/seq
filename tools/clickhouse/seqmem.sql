-- ClickHouse schema for seq memory-engine events emitted as JSONEachRow
-- (default output file: ~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl)

CREATE DATABASE IF NOT EXISTS seq;

CREATE TABLE IF NOT EXISTS seq.mem_events (
    ts_ms UInt64,
    dur_us UInt64,
    ok Bool,
    session_id String,
    event_id String,
    content_hash String,
    name LowCardinality(String),
    subject Nullable(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(toDateTime(ts_ms / 1000))
ORDER BY (session_id, ts_ms, event_id);

-- Trace events from the seq CLI/daemon (spans, logs, structured events)
CREATE TABLE IF NOT EXISTS seq.trace_events (
    ts_us Int64,
    app LowCardinality(String),
    pid UInt32,
    tid UInt64,
    level LowCardinality(String),
    kind LowCardinality(String),
    name String,
    message String,
    dur_us Int64
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(toDateTime(ts_us / 1000000))
ORDER BY (app, ts_us);

-- Materialized view: auto-parse tab-separated subject from context events
-- into queryable columns for window/app context analysis.
CREATE TABLE IF NOT EXISTS seq.window_context (
    ts_ms UInt64,
    dur_us UInt64,
    window_title String,
    bundle_id LowCardinality(String),
    url String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(toDateTime(ts_ms / 1000))
ORDER BY (bundle_id, ts_ms);

CREATE MATERIALIZED VIEW IF NOT EXISTS seq.mv_window_context TO seq.window_context AS
SELECT
    ts_ms,
    dur_us,
    splitByChar('\t', ifNull(subject, ''))[1] AS window_title,
    splitByChar('\t', ifNull(subject, ''))[2] AS bundle_id,
    splitByChar('\t', ifNull(subject, ''))[3] AS url
FROM seq.mem_events
WHERE name IN ('ctx.window', 'ctx.window.checkpoint');

-- Direct context table populated by seqd via native protocol
CREATE TABLE IF NOT EXISTS seq.context (
    ts_ms       UInt64,
    dur_ms      UInt64,
    app         LowCardinality(String),
    bundle_id   LowCardinality(String),
    window_title String,
    url         String DEFAULT '',
    afk         UInt8 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (app, ts_ms)
TTL toDateTime(ts_ms / 1000) + INTERVAL 90 DAY;

-- App usage: minutes per app per day (auto-updated on insert)
CREATE TABLE IF NOT EXISTS seq.app_minutes_daily (
    app         LowCardinality(String),
    day         Date,
    minutes     Float64
)
ENGINE = SummingMergeTree
ORDER BY (app, day);

CREATE MATERIALIZED VIEW IF NOT EXISTS seq.mv_app_minutes TO seq.app_minutes_daily AS
SELECT
    app,
    toDate(toDateTime(ts_ms / 1000)) AS day,
    dur_ms / 60000.0 AS minutes
FROM seq.context;

-- ─── Hive agent tables ──────────────────────────────────────────────────────

CREATE DATABASE IF NOT EXISTS hive;

-- Every superstep in every graph run
CREATE TABLE IF NOT EXISTS hive.supersteps (
    ts_ms           UInt64,
    thread_id       String,
    graph_name      LowCardinality(String),
    graph_version   UInt32,
    step_index      UInt32,
    frontier_count  UInt32,
    writes          UInt32,
    dur_us          UInt64,
    status          Enum8('started'=0, 'finished'=1, 'interrupted'=2, 'error'=3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (graph_name, thread_id, step_index);

-- Every LLM call (local MLX or cloud)
CREATE TABLE IF NOT EXISTS hive.model_invocations (
    ts_ms           UInt64,
    thread_id       String,
    node_id         LowCardinality(String),
    graph_name      LowCardinality(String),
    provider        LowCardinality(String),
    model           LowCardinality(String),
    input_tokens    UInt32,
    output_tokens   UInt32,
    dur_us          UInt64,
    ttft_us         UInt64,
    tool_calls      UInt16,
    ok              UInt8,
    error_msg       String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (provider, model, ts_ms);

-- Every tool execution
CREATE TABLE IF NOT EXISTS hive.tool_calls (
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

-- ─── Hive materialized views ────────────────────────────────────────────────

-- Token cost per model per hour (auto-updated)
CREATE TABLE IF NOT EXISTS hive.cost_per_hour (
    model           LowCardinality(String),
    hour            DateTime,
    input_tokens    UInt64,
    output_tokens   UInt64,
    calls           UInt64
)
ENGINE = SummingMergeTree
ORDER BY (model, hour);

CREATE MATERIALIZED VIEW IF NOT EXISTS hive.mv_cost_per_hour TO hive.cost_per_hour AS
SELECT
    model,
    toStartOfHour(toDateTime(ts_ms / 1000)) AS hour,
    input_tokens,
    output_tokens,
    1 AS calls
FROM hive.model_invocations;

-- Error hotspots: which nodes fail most (auto-updated)
CREATE TABLE IF NOT EXISTS hive.error_hotspots (
    graph_name  LowCardinality(String),
    node_id     LowCardinality(String),
    errors      UInt64
)
ENGINE = SummingMergeTree
ORDER BY (graph_name, node_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS hive.mv_error_hotspots TO hive.error_hotspots AS
SELECT
    graph_name,
    node_id,
    1 AS errors
FROM hive.model_invocations
WHERE ok = 0;

-- Tool error hotspots (auto-updated)
CREATE TABLE IF NOT EXISTS hive.tool_error_hotspots (
    tool_name   LowCardinality(String),
    errors      UInt64
)
ENGINE = SummingMergeTree
ORDER BY (tool_name);

CREATE MATERIALIZED VIEW IF NOT EXISTS hive.mv_tool_error_hotspots TO hive.tool_error_hotspots AS
SELECT
    tool_name,
    1 AS errors
FROM hive.tool_calls
WHERE ok = 0;
