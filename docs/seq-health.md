# Seq Health Check (`f seq-health`)

`f seq-health` runs strict end-to-end checks for training data reliability.

It validates:
- `seqd` control-plane connectivity (stream `seq ping` and datagram socket fallback)
- `seqd` launchd service presence and auto-repair (`launchctl kickstart -k`) when socket probes fail
- capture daemons running (`next_type`, `kar_signal`, `agent_qa`, watchdog)
- `seq_mem.jsonl` and `seq_trace.jsonl` parse health (tail parse)
- probe write+read in `seq_mem` (`seq.health.probe.v1`)
- active Kar pipeline probe (`cli.run.local` health source -> expect derived `kar.intent.v1` + `kar.outcome.v1`)
- required signal coverage in lookback window
- Kar intent/outcome linkage ratio
- watchdog report freshness
- ClickHouse TCP reachability (mode-aware: required in `native/mirror`, warning in `file`)

## Commands

From `~/code/seq`:

```bash
f seq-health
f seq-health-report
```

Relaxed mode (coverage checks become warnings):

```bash
f seq-health-relaxed
```

## Report

Default report path:
- `~/.local/state/seq/seq_health_report.json`

## Threshold tuning (Flow env)

- `SEQ_HEALTH_LOOKBACK_HOURS`
- `SEQ_HEALTH_TAIL_BYTES`
- `SEQ_HEALTH_MIN_NEXT_TYPE`
- `SEQ_HEALTH_MIN_KAR_INTENT`
- `SEQ_HEALTH_MIN_KAR_OUTCOME`
- `SEQ_HEALTH_MIN_AGENT_QA`
- `SEQ_HEALTH_MIN_KAR_LINK_RATE`
- `SEQ_HEALTH_WATCHDOG_MAX_AGE_MINUTES`
- `SEQ_HEALTH_REPAIR_WAIT_S`
- `SEQD_LAUNCHD_LABEL`

## Notes

- If `SEQ_CH_MODE=file`, local spool is the source of truth; ClickHouse TCP unreachability is warning-only.
- If `SEQ_CH_MODE=native` or `mirror`, ClickHouse reachability is treated as critical.
- Health probe rows are marked as `__seq_health_probe__` and are excluded from Kar training export.
- `f seq-health` now attempts one automatic `seqd` kickstart before failing control-plane checks.
