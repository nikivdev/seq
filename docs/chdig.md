# chdig (TUI) for seq Local Ingestor Queries

This doc shows the fastest path to:
1. install `chdig` into your PATH
2. start a local ClickHouse server that can read seq logs under `user_files/`
3. use `chdig`'s built-in SQL client to run "what did I do today?" style queries

## Install chdig into PATH

`chdig` is built from `~/repos/azat/chdig` and installed into `~/.flow/bin/chdig`.

```bash
cd /Users/nikiv/repos/azat/chdig
f deploy

which chdig
chdig --version
```

## Start a Local ClickHouse Server (for chdig)

`chdig` connects to a ClickHouse server over TCP, so for local log analysis we run
an ephemeral ClickHouse server that:
- listens on `127.0.0.1:9000` (native)
- exposes HTTP on `127.0.0.1:8123`
- sets `user_files_path` to `~/repos/ClickHouse/ClickHouse/user_files/`

```bash
cd /Users/nikiv/code/seq
./tools/clickhouse/local_server.sh start
```

Stop it when done:

```bash
./tools/clickhouse/local_server.sh stop
```

## Enter chdig and run SQL

Start `chdig` and jump into the SQL client:

```bash
chdig -u '127.0.0.1:9000' client
```

Inside the client, you can query seq's JSONEachRow files via ClickHouse's `file()`
table function. The files live in `user_files_path`, so you can refer to them by
basename:
- `seq_mem.jsonl`
- `seq_trace.jsonl`

## Common Views

These views make the rest of the queries shorter.

### `v_seq_mem`

Matches the schema in `tools/clickhouse/seqmem.sql`.

```sql
CREATE OR REPLACE VIEW v_seq_mem AS
SELECT
  ts_ms,
  dur_us,
  ok,
  session_id,
  event_id,
  content_hash,
  name,
  subject
FROM file(
  'seq_mem.jsonl',
  'JSONEachRow',
  'ts_ms UInt64, dur_us UInt64, ok Bool, session_id String, event_id String, content_hash String, name String, subject Nullable(String)'
);
```

### `v_window_segments`

`seqd` window context is emitted as:
`subject = "<windowTitle>\\t<bundleId>\\t<url>"`

```sql
CREATE OR REPLACE VIEW v_window_segments AS
SELECT
  toDateTime64(ts_ms / 1000.0, 3) AS ts,
  ts_ms,
  dur_us,
  ts_ms + intDiv(dur_us, 1000) AS end_ms,
  splitByChar('\t', ifNull(subject, ''))[1] AS window_title,
  splitByChar('\t', ifNull(subject, ''))[2] AS bundle_id,
  splitByChar('\t', ifNull(subject, ''))[3] AS url
FROM v_seq_mem
WHERE name IN ('ctx.window', 'ctx.window.checkpoint');
```

## Queries

### What Did I Do Today: Top Apps

```sql
WITH toStartOfDay(now()) AS d0
SELECT
  bundle_id,
  sum(dur_us) / 1e6 / 60 AS minutes
FROM v_window_segments
WHERE ts >= d0 AND ts < d0 + INTERVAL 1 DAY
GROUP BY bundle_id
ORDER BY minutes DESC
LIMIT 20;
```

### Zed Projects Today

This uses `window_title` as a proxy for "project".

```sql
WITH toStartOfDay(now()) AS d0
SELECT
  window_title,
  sum(dur_us) / 1e6 / 60 AS minutes
FROM v_window_segments
WHERE ts >= d0 AND ts < d0 + INTERVAL 1 DAY
  AND bundle_id IN ('dev.zed.Zed', 'dev.zed.Zed-Preview')
GROUP BY window_title
ORDER BY minutes DESC
LIMIT 50;
```

### Linsa Prompt Counts Today (provider/model)

Linsa records best-effort TRACE events into `seq_mem.jsonl` with:
- `name = linsa.prompt.user` / `linsa.prompt.assistant`
- `subject` contains `provider=...\\tmodel=...\\t...`

```sql
WITH toStartOfDay(now()) AS d0
SELECT
  extract(ifNull(subject, ''), 'provider=([^\\t]+)') AS provider,
  extract(ifNull(subject, ''), 'model=([^\\t]+)') AS model,
  count() AS prompts
FROM v_seq_mem
WHERE toDateTime64(ts_ms / 1000.0, 3) >= d0
  AND toDateTime64(ts_ms / 1000.0, 3) < d0 + INTERVAL 1 DAY
  AND name LIKE 'linsa.prompt.%'
GROUP BY provider, model
ORDER BY prompts DESC;
```

### Linsa Tool Call Counts Today

```sql
WITH toStartOfDay(now()) AS d0
SELECT
  replaceOne(name, 'linsa.tool.', '') AS tool,
  count() AS calls
FROM v_seq_mem
WHERE toDateTime64(ts_ms / 1000.0, 3) >= d0
  AND toDateTime64(ts_ms / 1000.0, 3) < d0 + INTERVAL 1 DAY
  AND name LIKE 'linsa.tool.%'
GROUP BY tool
ORDER BY calls DESC;
```

## Notes

- If your queries return zero rows, verify `seqd` is running and writing to:
  `~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl`.
- For large files, prefer creating MergeTree tables and ingesting with:
  `tools/clickhouse/seqmem_setup.sh` and `tools/clickhouse/seqmem_ingest.sh`.

