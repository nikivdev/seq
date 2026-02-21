# ClickHouse Mode Wrapper (`f ch-mode`)

Use this wrapper to switch seq ClickHouse emission mode without manually calling `f env set`.
Default mode is `file` (local spool only) for lowest user-path latency.

## Modes

- `native`: native ClickHouse writer only
- `mirror`: native writer + local JSONEachRow spool
- `file`: local JSONEachRow spool only
- `off`: disable ClickHouse emission

## Commands

Run from `~/code/seq`:

```bash
f ch-mode status
f ch-mode native
f ch-mode mirror
f ch-mode file
f ch-mode off
```

Equivalent shortcuts:

```bash
f ch-mode-status
f ch-mode-native
f ch-mode-mirror
f ch-mode-file
f ch-mode-off
```

## Set Remote Endpoint While Switching

You can set host/port/database in the same command:

```bash
f ch-mode mirror --host <remote-clickhouse-host> --port 9000 --database seq
```

This updates Flow env keys:

- `SEQ_CH_MODE`
- `SEQ_CH_HOST`
- `SEQ_CH_PORT`
- `SEQ_CH_DATABASE`

## Local Spool Paths

`mirror`/`file` modes write local JSON spool files via default paths unless overridden:

- mem spool default: `~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl`
- trace spool default: `~/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl`

Optional overrides:

```bash
f env set SEQ_CH_MEM_PATH=/absolute/path/seq_mem.jsonl
f env set SEQ_CH_LOG_PATH=/absolute/path/seq_trace.jsonl
```

Use absolute paths (do not use `~` in env values).

## Recommended Setup

For remote analytics with local durability:

```bash
f ch-mode mirror --host <remote-clickhouse-host> --port 9000 --database seq
f ch-mode status
```
