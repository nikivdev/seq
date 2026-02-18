# Seq Recipe Runtime Protocol

This document defines the seq-side protocol for low-latency recipe execution from Flow (including a MoonBit runtime).

Primary objective:
- support typed recipe commands without shell spawn
- keep transport and schema stable
- provide measurable timings for tuning to physical limits

## 1) Design principles

1. Fast by default: Unix domain sockets, fire-and-forget where possible.
2. Structured payloads: JSON envelope with versioning.
3. Backward compatible: existing `RUN ...` path remains supported.
4. Observable: include micro-timing and failure reason in responses.

## 2) Transport model

Primary transport:
- Unix datagram socket (dgram), low overhead for one-shot commands

Fallback transport:
- Unix stream socket for reliability/retries and larger payloads

Default endpoints (current local setup):
- dgram: `/tmp/seqd.sock.dgram`
- stream: `/tmp/seqd.sock`

Runtime policy:
1. try dgram send
2. if send fails or command requires ack, fallback to stream
3. bounded retry (max 1 reconnect for stream)

## 3) Protocol envelope (v1)

Request:

```json
{
  "v": 1,
  "id": "d41f1b80-60a9-4fb5-a4e8-3bd2f7be2f2f",
  "type": "batch",
  "commands": [
    { "type": "open_app", "app": "Safari" },
    { "type": "keystroke", "key": "1", "mods": ["ctrl"] }
  ],
  "meta": {
    "source": "flow-recipe",
    "recipe_id": "project:open-app-safari",
    "trace_id": "..."
  }
}
```

Response:

```json
{
  "v": 1,
  "id": "d41f1b80-60a9-4fb5-a4e8-3bd2f7be2f2f",
  "ok": true,
  "timing_us": {
    "recv": 8,
    "decode": 16,
    "dispatch": 24,
    "exec": 380,
    "total": 428
  }
}
```

Error response:

```json
{
  "v": 1,
  "id": "d41f1b80-60a9-4fb5-a4e8-3bd2f7be2f2f",
  "ok": false,
  "error": {
    "code": "unknown_command",
    "message": "type=open_url_in_app unsupported in v1"
  },
  "timing_us": { "recv": 9, "decode": 14, "total": 29 }
}
```

## 4) Command set (v1)

Supported commands:
- `open_app` `{ app, toggle? }`
- `open_with_app` `{ app, target }`
- `paste` `{ text }`
- `enter` `{ text }` (paste + return semantics)
- `keystroke` `{ key, mods[] }`
- `run_macro` `{ name }`

Batch command:
- `batch` wraps ordered `commands[]`
- default semantics: sequential execution

## 5) Execution semantics

Ordering:
- commands in one request are executed in order

Atomicity:
- v1 is best-effort sequential, not transactional rollback

Timeouts:
- receiver-level deadline
- per-command timeout optional in future versions

Idempotency:
- `id` is for tracing and dedupe support; dedupe optional in v1

## 6) C++ integration points in seq

Planned files:

```text
cli/cpp/src/recipe_protocol.hpp
cli/cpp/src/recipe_protocol.cpp
cli/cpp/src/recipe_receiver.hpp
cli/cpp/src/recipe_receiver.cpp
cli/cpp/src/actions_recipe.cpp
```

Responsibilities:
- `recipe_protocol.*`: parse/validate JSON, map to internal command structs
- `recipe_receiver.*`: socket receive loop, transport handling, timing capture
- `actions_recipe.cpp`: execute typed commands against existing action implementations

## 7) Backward compatibility

Keep current handlers:
- existing line protocol (`RUN ...`)
- existing user-command bridge behavior

Bridging rules:
- new protocol can internally map to existing action functions
- no immediate removal of old sockets/protocol path

## 8) Validation and security

Validation:
- strict schema checks (`v`, `type`, required fields)
- reject unknown command shapes by default
- payload size limits

Security:
- local Unix socket filesystem permissions remain primary boundary
- avoid executing arbitrary shell from protocol commands
- normalize/validate app paths and targets where applicable

## 9) Observability requirements

Every request should optionally emit trace events:
- `seqd.recipe.recv`
- `seqd.recipe.decode`
- `seqd.recipe.dispatch`
- `seqd.recipe.exec`
- `seqd.recipe.error` (if any)

All events should include:
- request `id`
- `recipe_id` and `source` from meta if present
- per-stage timing in microseconds

## 10) Benchmark methodology

Compare:
1. shell path (baseline)
2. current `RUN ...` socket path
3. recipe protocol dgram path
4. recipe protocol stream fallback path

Metrics:
- p50/p95/p99/mean
- failure count
- fallback count

Target:
- recipe protocol p95 should match or beat existing low-latency socket path for hot actions

## 11) Rollout phases

Phase A:
- implement parser + receiver behind feature flag
- support `open_app` only

Phase B:
- add `open_with_app`, `paste`, `enter`, `keystroke`
- add batch execution

Phase C:
- enable flow/MoonBit recipe runtime to use protocol by default for hot recipes

Rollback:
- disable recipe receiver feature flag
- clients automatically use existing socket/legacy path

## 12) Minimal smoke tests

1. decode/validate unit tests
2. Unix dgram integration test (`open_app`)
3. Unix stream fallback integration test
4. batch ordering test
5. malformed payload rejection test

Example manual send (dgram):

```bash
python3 - <<'PY'
import json, socket
msg = {
  "v": 1,
  "id": "manual-test",
  "type": "batch",
  "commands": [{"type": "open_app", "app": "Safari"}],
  "meta": {"source": "manual"}
}
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
s.connect("/tmp/seqd.sock.dgram")
s.send(json.dumps(msg).encode("utf-8"))
print("sent")
PY
```
