# Seq BlazingMQ Bridge (Low-Latency Kar -> Async Queue)

This integration adds an async queue submission path optimized for hotkeys:

1. Kar/CLI sends `BMQ_ENQ <payload>` to `seqd`.
2. `seqd` sends payload over local AF_UNIX datagram to `seq_bmq_bridge.py` and returns immediately.
3. If bridge is unavailable, `seqd` appends to local spool (`ts_ms\tpayload`) for later replay.
4. Bridge persists to SQLite, then dispatches in background.

## Why this keeps latency low

- UI path is fire-and-forget datagram (`seqd` does not wait for broker/network).
- Durable fallback exists even if bridge is down.
- Broker dispatch runs off hot path in a daemon.

## New seq command

```bash
seq bmq-enq '{"job":"summarize","repo":"/Users/nikiv/code/seq"}'
```

`seq bmq-enq` uses seqd datagram first (`/tmp/seqd.sock.dgram`) and falls back to stream RPC if needed.

## Flow tasks

```bash
f bmq-bridge-preflight
f bmq-bridge-on
f bmq-bridge-status
f bmq-enq '{"job":"index","path":"/Users/nikiv/code/myflow"}'
f bmq-bridge-logs
f bmq-bridge-off
```

## Backends

### Default (`jsonl`)

For local verification without broker:

- `SEQ_BMQ_BACKEND=jsonl`
- Output file: `${SEQ_BMQ_BRIDGE_OUT}` (default `~/.local/state/seq/bmq_bridge_out.jsonl`)

### Command backend

For real dispatch:

- `SEQ_BMQ_BACKEND=command`
- `SEQ_BMQ_DISPATCH_CMD='<command that reads payload from stdin>'`

Example with bundled bmqtool wrapper:

```bash
f env set SEQ_BMQ_BACKEND=command
f env set SEQ_BMQ_DISPATCH_CMD='python3 tools/seq_bmq_dispatch_bmqtool.py'
f env set SEQ_BMQ_QUEUE_URI='bmq://bmq.test.persistent.priority/jobs'
f env set SEQ_BMQ_BROKER='tcp://localhost:30114'
f bmq-bridge-restart
```

The wrapper sends `seqb64:<base64(payload)>` to avoid shell/CLI quoting issues.

## Suggested Kar usage

Use seq socket path (fastest path) or user-command bridge to run:

```bash
seq bmq-enq '{"intent":"review-push","repo":"/Users/nikiv/code/seq"}'
```

For very high frequency actions, keep payload compact (<3KB).

## Reliability model

- Bridge up: datagram -> SQLite queue -> async dispatch.
- Bridge down: `seqd` writes spool lines to `${SEQ_BMQ_BRIDGE_SPOOL}`.
- Bridge resumes: spool is imported automatically and drained.

## Env keys

- `SEQ_BMQ_BRIDGE_SOCKET`
- `SEQ_BMQ_BRIDGE_SPOOL`
- `SEQ_BMQ_BRIDGE_DB`
- `SEQ_BMQ_BRIDGE_PIDFILE`
- `SEQ_BMQ_BRIDGE_LOG`
- `SEQ_BMQ_BRIDGE_OUT`
- `SEQ_BMQ_BACKEND` (`jsonl` or `command`)
- `SEQ_BMQ_DISPATCH_CMD`
- `SEQ_BMQ_DISPATCH_TIMEOUT_S`
- `SEQ_BMQ_DISPATCH_BATCH`
- `SEQ_BMQ_POLL_SECONDS`
- `SEQ_BMQ_SPOOL_POLL_SECONDS`
- `SEQ_BMQ_BMQTOOL_BIN`
- `SEQ_BMQ_BROKER`
- `SEQ_BMQ_QUEUE_URI`
- `SEQ_BMQ_BMQTOOL_TIMEOUT_S`

## Notes

- This implementation prioritizes input latency and local durability.
- Delivery semantics are at-least-once from bridge queue; make consumers idempotent.
- If you need strict exactly-once semantics, add broker-side dedupe keys in payload.
