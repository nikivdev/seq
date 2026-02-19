# Seq Agent RPC v1

`seqd` now accepts JSON RPC requests over the existing Unix stream socket (`/tmp/seqd.sock` by default).

This gives agents a typed, low-overhead OS control path without shell command templating.

## Transport

- Socket: `--socket` path (default `/tmp/seqd.sock`)
- Frame: one JSON object per line (`\n` delimited)
- Request: UTF-8 JSON object with `op` and optional metadata/args
- Response: UTF-8 JSON object with consistent envelope

## Request shape

```json
{
  "op": "ping",
  "request_id": "req-123",
  "run_id": "run-abc",
  "tool_call_id": "tool-7",
  "args": {}
}
```

Notes:
- `args` is optional.
- For convenience, arguments can be top-level or inside `args`.

## Response shape

```json
{
  "ok": true,
  "op": "ping",
  "request_id": "req-123",
  "run_id": "run-abc",
  "tool_call_id": "tool-7",
  "ts_ms": 1771500000000,
  "dur_us": 21,
  "result": { "pong": true }
}
```

On failure:

```json
{
  "ok": false,
  "op": "open_app",
  "request_id": "req-999",
  "run_id": "run-abc",
  "tool_call_id": "tool-7",
  "ts_ms": 1771500000100,
  "dur_us": 35,
  "error": "missing_name"
}
```

## Supported ops (v1)

- `ping`
- `app_state`
- `perf`
- `open_app` (`name` or `app`)
- `open_app_toggle` (`name` or `app`)
- `run_macro` (`name` or `macro`)
- `click` (`x`, `y`)
- `right_click` (`x`, `y`)
- `double_click` (`x`, `y`)
- `move` (`x`, `y`)
- `scroll` (`x`, `y`, `dy`)
- `drag` (`x1`, `y1`, `x2`, `y2`)
- `screenshot` (optional `path`, default `/tmp/seq_screenshot.png`)

## CLI usage

```bash
seq rpc '{"op":"ping","request_id":"r1","run_id":"run1","tool_call_id":"t1"}'
seq rpc '{"op":"open_app","request_id":"r2","args":{"name":"Safari"}}'
seq rpc '{"op":"screenshot","request_id":"r3","args":{"path":"/tmp/agent.png"}}'
```

For short-lived test runs on machines where ClickHouse writer shutdown can delay CLI exit, use:

```bash
SEQ_CH_PORT=1 seq rpc '{"op":"ping","request_id":"test"}'
```

## Raw socket example

```bash
printf '{"op":"ping","request_id":"raw-1"}\n' | nc -U /tmp/seqd.sock
```
