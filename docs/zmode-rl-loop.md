# z-mode RL Loop (Using Existing seq + Prime-RL Infra)

This is the shortest operational loop to collect, gate, and prepare z-mode signal for RL training.

## 1) Keep capture running

From `~/code/seq`:

```bash
f seq-harbor-run
f kar-signal-capture-status
f seq-remote-health
```

## 2) Focus UX on z-mode experiments

```bash
f next-type-predictor-off
```

Or one command:

```bash
f zmode-experiment-prep
```

## 3) Export z-mode dataset

```bash
f zmode-signal-export
```

Bootstrap from full history (one-time):

```bash
f zmode-signal-export-all
```

Outputs:

- `${ZMODE_SIGNAL_OUT}/train.jsonl`
- `${ZMODE_SIGNAL_OUT}/val.jsonl`
- `${ZMODE_SIGNAL_OUT}/test.jsonl`
- `${ZMODE_SIGNAL_OUT}/summary.json`

## 4) Apply quality gates

```bash
f zmode-signal-audit
```

Default thresholds are strict production gates:

- decisions >= 500
- applies >= 200
- apply_rate >= 0.40
- joined >= 400
- link_rate >= 0.80
- action_dominance <= 0.55
- overrides >= 20
- failureish >= 60

## 5) Hand-off to training

When `f zmode-signal-audit` passes, use `train/val/test` as the RL dataset input for the z-mode policy scorer/assigner runs in Prime-RL.

The key contract is documented in:

- `docs/zmode-rl-signal-contract.md`
