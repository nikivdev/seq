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

-- ─── Agent full session capture (lossless + normalized) ───────────────────

-- Lossless JSONL line storage (for replay / reparse when formats change).
CREATE TABLE IF NOT EXISTS agent.raw_lines (
    ts_ms           UInt64,
    agent           LowCardinality(String), -- 'claude' or 'codex'
    session_id      String,
    source_path     String,
    source_offset   UInt64,                 -- byte offset in the source file
    line_type       LowCardinality(String),
    json            String                  -- full original JSON line
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (agent, session_id, ts_ms, source_offset);

-- Normalized message corpus for querying + RAG.
CREATE TABLE IF NOT EXISTS agent.messages (
    ts_ms           UInt64,
    agent           LowCardinality(String),
    session_id      String,
    message_id      String,
    role            LowCardinality(String), -- user/assistant/developer/system/tool
    kind            LowCardinality(String), -- text/reasoning/tool_use/tool_result/progress/snapshot
    model           LowCardinality(String),
    project_path    String,
    git_branch      String DEFAULT '',
    tool_name       LowCardinality(String) DEFAULT '',
    tool_call_id    String DEFAULT '',
    ok              UInt8 DEFAULT 1,
    text            String,
    json            String DEFAULT ''       -- optional: compact payload for tool blocks
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (agent, project_path, session_id, ts_ms);

-- ─── RAG tables over agent sessions ───────────────────────────────────────

CREATE DATABASE IF NOT EXISTS rag;

-- Canonical documents built from agent.messages rows.
CREATE TABLE IF NOT EXISTS rag.documents (
    doc_id           String,                 -- agent:session_id:message_id
    source           LowCardinality(String), -- 'agent_messages'
    ts_ms            UInt64,
    agent            LowCardinality(String),
    project_path     String,
    session_id       String,
    message_id       String,
    role             LowCardinality(String),
    kind             LowCardinality(String),
    title            String DEFAULT '',
    content          String,
    metadata_json    String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (project_path, ts_ms, doc_id);

-- Chunk index for retrieval (embedding optional at first).
CREATE TABLE IF NOT EXISTS rag.chunks (
    chunk_id         String,                -- doc_id:<chunk_index>
    doc_id           String,
    ts_ms            UInt64,
    agent            LowCardinality(String),
    project_path     String,
    session_id       String,
    role             LowCardinality(String),
    kind             LowCardinality(String),
    chunk_index      UInt32,
    chunk_text       String,
    chunk_hash       UInt64,
    embedding        Array(Float32) DEFAULT CAST([], 'Array(Float32)'),
    metadata_json    String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))
ORDER BY (project_path, ts_ms, doc_id, chunk_index);
