# Seq Capture Launchd Supervision

Use launchd to run capture daemons as always-on supervised services (auto-restart on crash/reboot).

Supervised services:
- `next_type_key_capture_daemon.py`
- `next_type_predictor_daemon.py`
- `kar_signal_capture.py`
- `agent_qa_ingest.py`
- `seq_signal_watchdog.py`

## Commands

From `~/code/seq`:

```bash
f seq-capture-launchd-install
f seq-capture-launchd-status
f seq-capture-launchd-restart
f seq-capture-launchd-stop
f seq-capture-launchd-uninstall
```

Convenience pipeline:

```bash
f seq-harbor-install
f seq-harbor-run
f seq-harbor-status
```

## Notes

- Install/restart automatically stops legacy background daemons first to avoid duplicate ingestion.
- Services are loaded under label prefix `dev.nikiv.seq-capture` by default.
- Watchdog remediation uses launchd kickstart labels by default.
- Logs go to `~/code/seq/cli/cpp/out/logs/*launchd.stderr.log`.
