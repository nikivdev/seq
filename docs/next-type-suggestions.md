# Next-Type Suggestions (Seq + Lin)

This documents the current foundation for "suggest next thing to type" in editor/agent sessions.

## What is implemented now

1. Seq native typing RPC ops (low overhead):
- `type_text` (`text`, optional `pid`)
- `replace_typed` (`delete_count`, `text`, optional `pid`)

Implemented in:
- `cli/cpp/src/seqd.mm`
- `cli/cpp/src/actions.mm`
- `docs/agent-rpc-v1.md`

2. Lin intent widget paste path prefers Seq RPC:
- `IntentNotificationService.handlePaste` now tries Seq Unix socket RPC first.
- Falls back to existing `AccessibilityService.type(...)` path if Seq is unavailable.

Implemented in:
- `~/code/org/linsa/lin/mac/Lin/Sources/IntentNotificationService.swift`

3. Seq test emitter for suggestion widgets:
- `tools/lin_next_type_suggest.py`
- Emits `kind=widget` intents with `action=paste` into Lin inbox JSONL.

4. Automatic watcher-driven suggestions:
- Claude/Codex completion watchers now extract command-like "next to type" candidates.
- Suggestions are emitted as `widget` intents with `action.type=paste`.
- Lin tracks next-type telemetry in batched JSONL writes:
  - `~/Library/Application Support/Lin/next-type-events.jsonl`
  - stages: `emitted`, `shown`, `accepted`, `dismissed`

5. Key-event batch ingester scaffold:
- `tools/next_type_key_event_ingest.py`
- consumes JSONL from stdin and writes stable `next_type_v1` spool rows in batches

6. Continuous key-event capture daemon:
- `tools/next_type_key_capture_daemon.py`
- tails headless tap log (`/tmp/cgeventtap.log`) and emits normalized `next_type.*` rows into `SEQ_CH_MEM_PATH`
- restart-safe via offset state file and background pidfile

7. Online predictor daemon + hotkey accept path:
- `tools/next_type_predictor_daemon.py`
  - learns completion/next-token priors from `next_type.key_down`
  - emits Lin widget suggestions with dedupe + cooldown
- `tools/next_type_accept.py`
  - accepts latest active suggestion via seq RPC (`type_text`)
  - emits accept telemetry (`next_type.suggestion_accept.v1`)

## Why this is better than paste-only fallback

- Seq path avoids clipboard churn and restore races.
- Seq path uses direct key event injection and supports targeted pid mode.
- Fallback keeps behavior safe when seqd is not running.
- Typing latency/reliability can be tuned per-machine:
  - `SEQ_TYPE_TEXT_DELAY_US` (default `800`)
  - `SEQ_TYPE_TEXT_CHUNK_UNITS` (default `64`)
  - `SEQ_TYPE_TEXT_MAX_BYTES` (default `16384`)

## Quick test

1. Ensure `seqd` is running.
2. Ensure Lin app is running with intent inbox watcher enabled.
3. Emit a suggestion:

```bash
cd ~/code/seq
./tools/lin_next_type_suggest.py "git status && f ai:review"
```

4. Accept widget action in Lin.
5. Expected:
- Text is typed into the focused app.
- Lin log shows `paste ok via seq rpc`.

## Continuous Capture (one command)

Run from `~/code/seq`:

```bash
f seq-harbor-install
f seq-harbor-run
f seq-harbor-status
f next-type-accept
f next-type-accept-dry
```

This keeps capture always-on with:
- `next-type` keystroke stream (`next_type.*` events in `seq_mem.jsonl`)
- online next-type prediction (`next_type.suggestion_emit.v1`)
- Claude/Codex Q/A capture (`agent.qa.pair` events)
- local file-mode capture for minimal user latency

Useful ops:

```bash
f seq-harbor-logs
f next-type-capture-status
f next-type-capture-off
f next-type-predictor-status
f next-type-predictor-off
```

Operational runbook:
- `docs/seq-harbor-capture.md`
- `docs/kar-rl-signal-contract.md`
- `docs/seq-signal-watchdog.md`

## RPC examples

```bash
seq rpc '{"op":"type_text","args":{"text":"hello from seq"}}'
seq rpc '{"op":"replace_typed","args":{"delete_count":5,"text":"world"}}'
```

## Next phases

1. Ranking
- Rule-first ranking (fast deterministic), optional LLM rerank only when ambiguous.

2. Learning loop
- Record accept/dismiss outcomes and feed into action graph for personalized ranking.

3. UI refinement
- Add explicit "Type", "Copy", "Dismiss" controls and confidence hints in widget.

4. RL training integration
- Batch-export telemetry + keystroke context to ClickHouse and build `prime-rl` datasets.
- See: `docs/next-type-rl-optimized-request.md`

## Guardrails

- Keep suggestion dedupe by stable id/context hash.
- Hard cap injected text length for accidental large payloads.
- Always preserve fallback path when seqd is unavailable.
- Lin dedupes repeated next-type telemetry rows by `stage + intent_id/completion_key`.
