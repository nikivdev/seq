# Seq Signal Watchdog

The watchdog validates that your data capture pipeline is both running and producing usable RL signal.

It checks:
- daemon liveness (`next_type`, `kar_signal`, `agent_qa`)
- recent signal counts from `seq_mem`
- Kar intent/outcome linkage quality (`kar_outcome / kar_intent`)
- optional auto-remediation (launchd kickstart or direct daemon restart)
- periodic capture checkpoints (manifests + state/report copies)
- writes a report JSON and optional `seq.signal.health.v1` events

## Commands

From `~/code/seq`:

```bash
f seq-signal-watchdog-preflight
f seq-signal-watchdog-on
f seq-signal-watchdog-status
f seq-signal-watchdog-logs
f seq-signal-watchdog-report
f seq-checkpoints-list
f seq-checkpoints-latest
```

Run one check immediately:

```bash
f seq-signal-watchdog-once
```

## Report output

Default:
- `~/.local/state/seq/signal_watchdog_report.json`

Includes:
- daemon states
- counts for `next_type.*`, `kar.intent.v1`, `kar.outcome.v1`, `kar.override.v1`, `agent.qa.pair`
- `kar_link_rate`
- remediation attempts/outcomes
- snapshot metadata
- `overall_pass`

## Tuning knobs (Flow env)

- `SEQ_SIGNAL_WATCHDOG_INTERVAL_SECONDS` (default `900`)
- `SEQ_SIGNAL_WATCHDOG_LOOKBACK_HOURS` (default `24`)
- `SEQ_SIGNAL_WATCHDOG_TAIL_BYTES` (default `20971520`)
- `SEQ_SIGNAL_WATCHDOG_MIN_KAR_INTENTS` (default `20`)
- `SEQ_SIGNAL_WATCHDOG_MIN_KAR_LINK_RATE` (default `0.75`)
- `SEQ_SIGNAL_WATCHDOG_EMIT_EVENT` (default `true`)
- `SEQ_SIGNAL_WATCHDOG_AUTO_REMEDIATE` (default `true`)
- `SEQ_SIGNAL_WATCHDOG_REMEDIATE_WITH_LAUNCHD` (default `true`)
- `SEQ_SIGNAL_WATCHDOG_REMEDIATE_COOLDOWN_SECONDS` (default `120`)
- `SEQ_SIGNAL_WATCHDOG_LABEL_NEXT_TYPE` (default `dev.nikiv.seq-capture.next-type`)
- `SEQ_SIGNAL_WATCHDOG_LABEL_KAR_SIGNAL` (default `dev.nikiv.seq-capture.kar-signal`)
- `SEQ_SIGNAL_WATCHDOG_LABEL_AGENT_QA` (default `dev.nikiv.seq-capture.agent-qa`)
- `SEQ_SIGNAL_WATCHDOG_SNAPSHOT_ENABLED` (default `true`)
- `SEQ_SIGNAL_WATCHDOG_SNAPSHOT_DIR` (default `~/.local/state/seq/checkpoints`)
- `SEQ_SIGNAL_WATCHDOG_SNAPSHOT_INTERVAL_MINUTES` (default `30`)
- `SEQ_SIGNAL_WATCHDOG_SNAPSHOT_KEEP` (default `48`)
- `SEQ_SIGNAL_WATCHDOG_SNAPSHOT_TAIL_BYTES` (default `1048576`)

## Notes

- `overall_pass=false` does not necessarily mean your machine is broken; it can also mean low activity in lookback.
- Use `f kar-signal-backfill` to bootstrap Kar signal history before first training export.
