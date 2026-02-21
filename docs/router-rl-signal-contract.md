# Flow Router RL Signal Contract (seq)

This document defines the **minimum useful** signal to collect for Flow router RL.

If you only collect generic traces (`cli.open_app_toggle.*`, app switches, random Q/A text), you will train on noise.

## Verdict on the current plan

The plan from `harbor/docs/claude-codex-session-practical-loop.md` and `prime-rl/docs/flow-rise-holy-grail-run.md` is valid, with one hard requirement:

- data must include **linked ground truth**: `decision_id -> chosen_task -> observed outcome`.

Without this linkage, labels are proxy-only and training quality is not trustworthy.

## Required events (must-have)

Emit these events into `seq_mem.jsonl`:

1. `flow.router.decision.v1`
2. `flow.router.outcome.v1`
3. `flow.router.override.v1` (when user overrides routing)

All three are emitted with `tools/router_signal_emit.py`.

## Schema fields

### Decision (`flow.router.decision.v1`)

Required:

- `decision_id`
- `session_id`
- `chosen_task`
- `confidence`
- `user_intent`

Recommended context:

- `project_fingerprint`, `project_path`, `git_branch`, `git_commit`
- `context` JSON (changed files, failing tests, CI status, recent errors)
- `candidates` JSON list

### Outcome (`flow.router.outcome.v1`)

Required:

- `decision_id`
- `outcome` (`success|partial|failure|wasted`)
- `task_executed`
- `time_to_resolution_ms`

Recommended:

- `manual_override_task`
- `error_kind`

### Override (`flow.router.override.v1`)

Required:

- `decision_id`
- `original_task`
- `override_task`

Recommended:

- `reason`

## Why this avoids useless signal

- It records **what was chosen** and **what actually happened**.
- It captures counterfactual corrections via override events.
- It supports objective reward mapping and hard-negative mining.
- It enables meaningful gates (link-rate, dominance, failure coverage).

## Tooling added in seq

- Emit events: `tools/router_signal_emit.py`
- Export joined dataset + split + summary: `tools/router_signal_export.py`
- Audit quality gates: `tools/router_signal_audit.py`

Flow tasks:

- `f router-signal-emit-sample`
- `f router-signal-export`
- `f router-signal-audit`

Exporter reliability features:
- each row includes deterministic `dedupe_id`
- duplicate `dedupe_id` rows are dropped before split
- `summary.json` reports duplicate source IDs and dropped duplicate rows

## End-to-end loop

1. Flow emits decision/outcome/override via `router_signal_emit.py`.
2. seq continuously appends to `seq_mem.jsonl` (low-latency file mode is fine).
3. Export dataset:
   - `f router-signal-export`
4. Run quality gates:
   - `f router-signal-audit`
5. Only train when audit passes.

## Integration hook in Flow (recommended)

At routing decision point:

```bash
python3 ~/code/seq/tools/router_signal_emit.py decision \
  --decision-id "$DECISION_ID" \
  --chosen-task "$CHOSEN_TASK" \
  --confidence "$CONF" \
  --user-intent "$USER_INTENT" \
  --context-json "$CONTEXT_JSON"
```

After task completes:

```bash
python3 ~/code/seq/tools/router_signal_emit.py outcome \
  --decision-id "$DECISION_ID" \
  --outcome "$OUTCOME" \
  --task-executed "$TASK_EXECUTED" \
  --time-to-resolution-ms "$TTR_MS" \
  --manual-override-task "$OVERRIDE_TASK"
```

If user overrides:

```bash
python3 ~/code/seq/tools/router_signal_emit.py override \
  --decision-id "$DECISION_ID" \
  --original-task "$ORIGINAL_TASK" \
  --override-task "$OVERRIDE_TASK" \
  --reason "$REASON"
```

## Initial gate targets

Use as minimum launch gates:

- `decisions >= 500`
- `joined_rows >= 400`
- `decision_outcome_link_rate >= 0.80`
- `task_dominance <= 0.55`
- `overrides >= 20`
- `failureish (partial+failure+wasted) >= 60`

If these fail, do not run RL. Fix telemetry first.
