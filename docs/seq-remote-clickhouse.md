# Seq Remote ClickHouse Capture

Use this when you want seq RL signals written primarily to remote ClickHouse (Linux host), while keeping only a bounded local tail.

## Why

- avoid unbounded growth of `~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl`
- keep low-latency local capture path
- keep reliability when remote host is briefly unavailable

## How it works

Python signal writers (`next_type`, `kar`, `agent_qa`, context/predictor events) now use `tools/seq_mem_sink.py`:

- `SEQ_MEM_SINK_MODE=file`: local JSONL only
- `SEQ_MEM_SINK_MODE=remote`: remote ClickHouse first, fallback queue if remote down
- `SEQ_MEM_SINK_MODE=dual`: remote + local JSONL
- `SEQ_MEM_SINK_MODE=auto`: remote when `SEQ_MEM_REMOTE_URL` is set, otherwise file

Remote failure handling:
- rows are queued at `${SEQ_MEM_REMOTE_FALLBACK_PATH}`
- replay queue with `f seq-mem-sink-drain`

Local disk control in remote mode:
- `${SEQ_MEM_LOCAL_TAIL_ENABLED}` (default `true`)
- `${SEQ_MEM_LOCAL_TAIL_MAX_BYTES}` (default `52428800` = 50MB)

## Setup

```bash
cd ~/code/seq
f env set --personal SEQ_MEM_REMOTE_URL=http://<linux-host>:8123
f env set --personal SEQ_CH_HOST=<linux-host>
f env set --personal SEQ_CH_PORT=9000
f env set --personal SEQ_CH_HTTP_PORT=8123
f env set --personal SEQ_CH_DATABASE=seq

f rl-capture-remote-on
f seq-harbor-run
f seq-mem-sink-status
```

## Verify

```bash
f seq-checkpoint-now-and-delta
f seq-mem-sink-status
```

If fallback queue is non-empty after network recovery:

```bash
f seq-mem-sink-drain
```
