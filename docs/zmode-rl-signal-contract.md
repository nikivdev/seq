# z-mode RL Signal Contract

This contract links z-mode policy decisions to downstream Kar outcomes so RL can optimize real utility.

## Source events

- `zmode.policy.decision.v1`
- `zmode.policy.apply.v1`
- `seqd.run`
- `cli.run.local`
- `seqd.open_app_toggle`
- `cli.open_app_toggle.action`
- `app.activate`

## Derived events

`tools/kar_signal_capture.py` now emits:

1. `kar.intent.v1`
2. `kar.outcome.v1`
3. `kar.override.v1`
4. `zmode.policy.override.v1`

## Join keys now attached to `kar.*`

Each `kar.intent/outcome/override` subject includes:

- `mapping_epoch_id`
- `zmode_policy_decision_id`
- `candidate_id`
- `candidate_key`
- `candidate_action`
- `policy_apply_ts_ms`

This enables direct policy->outcome joins without fragile timestamp-only matching.

## Explicit correction signal

When one Kar intent supersedes another inside override window, capture emits:

- `kar.override.v1`
- `zmode.policy.override.v1` (new)

`zmode.policy.override.v1` includes old/new action/candidate linkage and timing (`ms_since_apply`, `ms_since_decision`).

## Export + audit loop

From `~/code/seq`:

```bash
f zmode-signal-export
f zmode-signal-audit
```

Outputs:

- `${ZMODE_SIGNAL_OUT}/train.jsonl`
- `${ZMODE_SIGNAL_OUT}/val.jsonl`
- `${ZMODE_SIGNAL_OUT}/test.jsonl`
- `${ZMODE_SIGNAL_OUT}/summary.json`

Quality gates:

- decision volume
- apply rate
- decision->outcome link rate
- override volume
- action dominance
- failureish coverage (`partial+failure+wasted`)

## Experiment focus mode

To avoid UX noise while validating z-mode:

```bash
f zmode-experiment-prep
```

This disables next-type predictor suggestions and runs export/audit gates.
