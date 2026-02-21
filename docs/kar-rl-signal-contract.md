# Kar RL Signal Contract

This document defines RL-grade signal derived from your real Karabiner usage.

## Source events (already emitted by seq)

- `seqd.run`
- `cli.run.local`
- `seqd.open_app_toggle`
- `cli.open_app_toggle.action`
- `app.activate`

## Derived events (new)

The daemon `tools/kar_signal_capture.py` emits:

1. `kar.intent.v1`
2. `kar.outcome.v1`
3. `kar.override.v1`

### Intent (`kar.intent.v1`)

Represents a Kar-triggered action decision.

Core fields in `subject`:
- `decision_id`
- `source_event_id`, `source_event_name`
- `session_id`
- `action_type` (`run_macro` or `open_app_toggle`)
- `action_name`
- `macro_name`
- `target_app`, `front_app`, `prev_app`

### Outcome (`kar.outcome.v1`)

Represents utility of a Kar intent.

Core fields in `subject`:
- `decision_id`
- `outcome` (`success`, `partial`, `failure`, `wasted`)
- `latency_ms`
- `observed_app`
- `reason`

Current policy:
- `seqd.run` / `cli.run.local`: `partial` if `ok=true`, else `failure`
- `open_app_toggle`: success when expected target app activates within `SEQ_KAR_SIGNAL_OUTCOME_WINDOW_MS`
- unresolved intents become `wasted`
- superseded intents become `wasted`

### Override (`kar.override.v1`)

Represents a user correction/supersession.

Core fields in `subject`:
- `decision_id`
- `override_decision_id`
- `ms_since_decision`
- `reason`

## Capture and export tasks

From `~/code/seq`:

```bash
f kar-signal-capture-preflight
f kar-signal-capture-on
f kar-signal-capture-status
f kar-signal-capture-logs
```

Behavior:
- background `start/run` tails from current EOF on first boot (no historical replay)
- use `f kar-signal-capture-once` when you explicitly want backfill from current saved offset
- use `f kar-signal-backfill` to derive from the start of current `seq_mem`

Dataset build + quality gates:

```bash
f kar-signal-export
f kar-signal-export-all   # bootstrap from full history
f kar-signal-audit
```

Outputs:
- `${KAR_SIGNAL_OUT}/train.jsonl`
- `${KAR_SIGNAL_OUT}/val.jsonl`
- `${KAR_SIGNAL_OUT}/test.jsonl`
- `${KAR_SIGNAL_OUT}/summary.json`

Exporter reliability features:
- each row carries a deterministic `dedupe_id`
- exporter drops duplicate `dedupe_id` rows before split
- `summary.json` reports duplicate source IDs and dropped duplicate row counts

Continuous validation:
- `f seq-signal-watchdog-on`
- `f seq-signal-watchdog-report`
- See `docs/seq-signal-watchdog.md`

## Why this is useful for RL

This is not synthetic labeling. It captures your real shortcut decisions, observed outcomes, and explicit overrides under real workload context.

That gives a direct path to train policies that optimize your actual Kar+Seq operating behavior.
