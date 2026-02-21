# Next-Type RL: Optimized Request (Seq + Lin + Prime-RL)

Use this when you want an implementation agent to build the full training loop for:

1. next character prediction
2. next word prediction
3. next phrase/command prediction

with strict low-latency constraints for interactive coding sessions.

## Copy-paste request

```text
Goal:
Build a production next-type system on top of existing Seq/Lin/Prime-RL infra.
System must suggest the next character/word/phrase in coding sessions (Zed/Codex/Claude) and improve from real user behavior.

Hard requirements:
1) zero added user latency (all logging async + batched)
2) no blocking on network in typing path
3) event schemas are stable and versioned
4) support offline spool + replay to ClickHouse
5) train/eval/deploy loop in Prime-RL with holdout gates

Use these repos:
- /Users/nikiv/code/seq
- /Users/nikiv/code/org/linsa/lin
- /Users/nikiv/repos/PrimeIntellect-ai/prime-rl

Deliverables:
A) Data capture
- Add key event capture adapter (osx-event-observer-based) that emits compact key events with context:
  session_id, source, project_path, app_id, cursor_state_hash, prefix_text_hash, suggestion_id (if any), timestamp_ms.
- Batch writes to local spool (JSONL), flush every 50 events or 2s.
- Separate high-frequency key events from lower-frequency context snapshots.

B) Context join
- Join watcher context (Claude/Codex completion summaries, proposed suggestion text, accept/dismiss outcomes)
  with key event streams by session_id + time window.
- Produce training rows for:
  1) char-level continuation
  2) word-level continuation
  3) phrase/command continuation

C) Filtering
- Keep all user-typed outcomes; no hard exclusion.
- Mark noise with soft quality score instead of dropping rows.
- Deduplicate exact repeats by context hash + target hash + 5s bucket.

D) Dataset builder
- Output parquet/jsonl splits: train/val/test by time (not random) to avoid leakage.
- Minimum fields:
  input_prefix, target_text, granularity(char|word|phrase), context_features, policy_source, reward_proxy.

E) Prime-RL integration
- Add next-type environment/configs under examples/flow_rise with deterministic splits.
- Promotion gates:
  - acceptance-weighted top-1 improvement on holdout
  - latency budget unchanged
  - no regression on phrase-level command correctness

F) Inference path
- Keep suggestion generation local/rule-first where possible.
- Optional model rerank only when ambiguous, with strict timeout budget.
- Fallback to current behavior if model is unavailable.

Validation:
- p50 suggestion generation < 20ms
- p95 accept->type injection < 80ms
- event loss rate < 0.1%
- canary win-rate > baseline before broad rollout
```

## Data schema contract (v1)

- `event_type`: `key_burst|context_snapshot|suggestion_emitted|suggestion_shown|suggestion_accepted|suggestion_dismissed`
- `schema_version`: `next_type_v1`
- `session_id`
- `source`: `claude|codex|zed|other`
- `project_path`
- `app_id`
- `timestamp_ms`
- `payload` (event-specific JSON object)

## Suggested batching defaults

- key bursts: flush every `50` events or `2s`
- context snapshots: flush every `1` event (low rate)
- suggestion events: flush every `16` events or `2s`

## Starter ingestion command

Use the provided batch ingester to avoid per-keystroke I/O overhead:

```bash
python3 tools/next_type_key_event_ingest.py \
  --source zed \
  --session-id <session_id> \
  --project-path <project_path>
```

Pipe osx-event-observer JSONL output into this process.

## Why this fits existing infra

- Seq already handles low-latency local actuation and telemetry paths.
- Lin already has watcher context and widget acceptance signals.
- Prime-RL already has gating, monitoring, and deploy workflows.

This plan only adds missing dataset contracts and training loop glue.
