# Seq Harbor Continuous Capture

This is the operator path for always-on high-signal data collection from your Mac into seq spool files, for downstream RL dataset builds.

## Goal

Capture continuously with near-zero user impact:
- keystroke-level interaction signal (`next_type.*`)
- predicted completion signal (`next_type.suggestion_emit/accept`)
- Kar decision/outcome/override signal (`kar.*`)
- Claude/Codex task dialogue signal (`agent.qa.pair`)
- existing seq traces/mem events in file mode

## One-time setup

From `~/code/seq`:

```bash
f seq-harbor-install
```

What this does:
- forces `SEQ_CH_MODE=file` for low-latency local spool writes
- validates key-capture prerequisites (`cgeventtap` binary, paths, ingest helper)
- installs launchd supervision for all capture daemons

## Start continuous capture

```bash
f seq-harbor-run
```

This (re)starts launchd-supervised capture services:
- `next_type_key_capture_daemon.py` (`next_type.*` keystroke events)
- `next_type_predictor_daemon.py` (OS-level completion suggestions + accept telemetry)
- `kar_signal_capture.py` (`kar.intent/outcome/override`)
- `agent_qa_ingest.py` (`agent.qa.pair`)
- `seq_signal_watchdog.py` (health, auto-remediation, periodic checkpoints)

## Status and logs

```bash
f seq-harbor-status
f seq-harbor-logs
f seq-health
f next-type-accept
```

Launchd-only status:

```bash
f seq-capture-launchd-status
```

## Event output

Primary sink:
- `${SEQ_CH_MEM_PATH}` (default `~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl`)

Expected event names:
- `next_type.key_down`
- `next_type.key_up`
- `next_type.flags_changed`
- `next_type.suggestion_emit.v1`
- `next_type.suggestion_accept.v1`
- `kar.intent.v1`
- `kar.outcome.v1`
- `kar.override.v1`
- `agent.qa.pair`

## Stop capture

```bash
f seq-harbor-stop
```

## Notes

- `cgeventtap-example` is listen-only; capture happens out-of-band and should not add typing latency.
- If key events are missing, verify macOS Accessibility/Input Monitoring permissions for the event-tap app.
- For warm-start behavior, key capture stores offsets in `SEQ_NEXT_TYPE_STATE`.
- For first-time Kar dataset bootstrapping, run `f kar-signal-backfill` once.
- For launchd internals and labels, see `docs/seq-capture-launchd.md`.
