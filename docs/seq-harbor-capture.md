# Seq Harbor Continuous Capture

This is the operator path for always-on high-signal data collection from your Mac into seq spool files, for downstream RL dataset builds.

## Goal

Capture continuously with near-zero user impact:
- keystroke-level interaction signal (`next_type.*`)
- predicted completion signal (`next_type.suggestion_emit/accept`)
- Kar decision/outcome/override signal (`kar.*`)
- Claude/Codex task dialogue signal (`agent.qa.pair`)
- existing seq traces/mem events in file mode
- hosted Maple export for all seq mem/trace rows (`seq_maple_forwarder.py`)

## One-time setup

From `~/code/seq`:

```bash
f seq-harbor-install
```

What this does:
- forces `SEQ_CH_MODE=file` for low-latency local spool writes
- validates key-capture prerequisites (`cgeventtap` binary, paths, ingest helper)
- installs launchd supervision for all capture daemons

### Remote-first mode (Linux ClickHouse host)

If local disk usage is a problem, switch Python signal writers to remote ClickHouse:

```bash
# Required
f env set --personal SEQ_MEM_REMOTE_URL=http://<linux-host>:8123
f env set --personal SEQ_CH_HOST=<linux-host>
f env set --personal SEQ_CH_PORT=9000
f env set --personal SEQ_CH_DATABASE=seq

# Enable remote-first capture (explicit force guard)
f seq-harbor-install-remote
f seq-harbor-run
f seq-mem-sink-status
```

Behavior in remote mode:
- writers send `JSONEachRow` directly to `${SEQ_MEM_REMOTE_URL}` (`seq.mem_events`)
- if remote is down, rows are queued locally at:
  - `${SEQ_MEM_REMOTE_FALLBACK_PATH}` (default `~/.local/state/seq/remote_fallback/seq_mem_fallback.jsonl`)
- local seq mem tail is bounded by:
  - `${SEQ_MEM_LOCAL_TAIL_ENABLED}` + `${SEQ_MEM_LOCAL_TAIL_MAX_BYTES}` (default 50MB)
- recover queued rows later with:

```bash
f seq-mem-sink-drain
```

Detailed reference:
- `docs/seq-remote-clickhouse.md`
- `docs/seq-maple-forwarder.md`

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
- `seq_maple_forwarder.py` (ships local `seq_mem`/`seq_trace` rows to hosted Maple)

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
- `next_type.text_burst.v1`
- `next_type.context.v1`
- `next_type.suggestion_emit.v1`
- `next_type.suggestion_accept.v1`
- `kar.intent.v1`
- `kar.outcome.v1`
- `kar.override.v1`
- `zmode.policy.decision.v1`
- `zmode.policy.apply.v1`
- `zmode.policy.override.v1`
- `agent.qa.pair`

## Checkpoint deltas (see data with your own eyes)

The watchdog writes periodic checkpoints under:
- `${SEQ_SIGNAL_WATCHDOG_SNAPSHOT_DIR}` (default `~/.local/state/seq/checkpoints`)

Use these to inspect what changed between snapshots:

```bash
f seq-checkpoints-list
f seq-checkpoint-now-and-delta
f seq-checkpoints-delta
f seq-checkpoints-delta-watch
```

`seq-checkpoints-delta` shows:
- file growth (`seq_mem` / `seq_trace`)
- delta of manifest signal counters
- actual event counts between checkpoint timestamps
- high-signal counts (`next_type.*`, `kar.*`, `agent.qa.pair`, router signals)

## Stop capture

```bash
f seq-harbor-stop
```

## Notes

- The headless tap is listen-only; capture happens out-of-band and should not add typing latency.
- Capture uses `seq-cgeventtap-headless` by default (no window/UI popup).
- If key events are missing, verify macOS Accessibility/Input Monitoring permissions for the tap binary.
- Auto-relaunch of the tap binary is rate-limited by `SEQ_NEXT_TYPE_TAP_RESTART_COOLDOWN_S` (default 60s) to avoid prompt storms.
- For warm-start behavior, key capture stores offsets in `SEQ_NEXT_TYPE_STATE`.
- For first-time Kar dataset bootstrapping, run `f kar-signal-backfill` once.
- For launchd internals and labels, see `docs/seq-capture-launchd.md`.
