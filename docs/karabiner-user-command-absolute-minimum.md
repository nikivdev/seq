# Karabiner User Command Absolute-Minimum Setup

This runbook is for lowest possible latency with Karabiner `send_user_command` + `seqd`.

Target setup:
- bridge is a prebuilt **release** binary
- bridge is **always running** via `launchd`
- bridge runs with high-priority scheduling hints (`userInteractive`)
- no `swift run` in the hot path
- seqd open-app path uses in-memory front-app cache first (fresh-cache fast path)

## 1) One-time setup (or after bridge code changes)

From `~/code/seq`:

```bash
f kar-uc-build-bridge
f kar-uc-launchd-install
f kar-uc-launchd-status
```

What this does:
- builds `~/repos/pqrs-org/Karabiner-Elements-user-command-receiver/.build/release/seq-user-command-bridge`
- installs `~/Library/LaunchAgents/dev.nikiv.seq-user-command-bridge.plist`
- starts/restarts the agent with:
  - `SEQ_USER_COMMAND_SOCKET_PATH=/Library/Application Support/org.pqrs/tmp/user/<uid>/user_command_receiver.sock`
  - `SEQ_SOCKET_PATH=/tmp/seqd.sock`
  - `SEQ_DGRAM_SOCKET_PATH=/tmp/seqd.sock.dgram`
  - `SEQ_BRIDGE_HIGH_PRIORITY=1`

Bridge logs:
- `~/code/seq/cli/cpp/out/logs/kar_uc_bridge.stdout.log`
- `~/code/seq/cli/cpp/out/logs/kar_uc_bridge.stderr.log`

Tail logs:

```bash
f kar-uc-launchd-logs
```

## 2) After reboot (fast bring-up)

From `~/code/seq`:

```bash
f daemon-restart
f kar-uc-launchd-status
kar
```

Notes:
- `kar` regenerates `~/.config/karabiner/karabiner.json` from `~/.config/kar/config.ts`.
- If your app key mappings changed, always run `kar` before testing latency.

## 3) Verify config path is active

Check generated Karabiner rules:

```bash
rg -n 'send_user_command|shell_command|open_app_toggle|OPEN_WITH_APP|Ghostty|Safari' ~/.config/karabiner/karabiner.json | head -n 80
```

If you are doing A/B:
- legacy path key should show `shell_command`
- new path key should show `send_user_command`

## 4) Verify low-latency path end-to-end

In terminal A:

```bash
f kar-uc-launchd-logs
```

In terminal B:

```bash
tail -f ~/code/seq/cli/cpp/out/logs/trace.log | rg "seqd.open_app_toggle|seqd.open_with_app|cli.open_app_toggle.action"
```

Then trigger your keys.

Expected:
- bridge receives immediately
- `seqd.open_app_toggle` or `seqd.open_with_app` appears immediately after
- no extra process spawn in hot path

## 5) seqd low-latency knobs (openApp / zed relevant)

The current defaults are tuned for low latency:

- `SEQ_OPEN_APP_FORCE_OS_FRONT_QUERY=0` (default)
  - do not force NSWorkspace frontmost query on every toggle
  - trust in-memory observer cache when recent

- `SEQ_OPEN_APP_FRONT_CACHE_MAX_AGE_MS=120` (default)
  - max age of cached front-app state used for fast path
  - smaller value = more correctness checks, slightly more overhead

- `SEQ_OPEN_APP_ALLOW_SEQMEM_PREV_FALLBACK=0` (default)
  - disables disk/log scan fallback in hot path
  - removes occasional tail-latency spikes when previous-app is unknown

- `SEQ_ACTION_STAGE_TRACE=0` (default)
  - when set to `1`, emits fine-grained stage timing events:
    - `actions.open_app.stage`
    - `actions.open_with_app.stage`

You can override for experiments:

```bash
export SEQ_OPEN_APP_FORCE_OS_FRONT_QUERY=1
export SEQ_OPEN_APP_FRONT_CACHE_MAX_AGE_MS=50
export SEQ_OPEN_APP_ALLOW_SEQMEM_PREV_FALLBACK=1
export SEQ_ACTION_STAGE_TRACE=1
f daemon-restart
```

## 6) Benchmark (bridge vs direct)

Run:

```bash
f kar-uc-bench --iterations 300 --warmup 40 --bridge-bin ~/repos/pqrs-org/Karabiner-Elements-user-command-receiver/.build/release/seq-user-command-bridge --json-out /tmp/kar_uc_absmin.json
```

Interpretation:
- bridge overhead should remain microseconds to low tens of microseconds
- if perceived latency is still high, bottleneck is app activation/window server/system load, not bridge IPC

## 7) Keep test environment clean

For trustworthy latency comparisons:
- avoid heavy foreground workloads (browser renderers, simulators, large IDE indexing)
- avoid changing bridge binaries while testing
- avoid running debug bridge (`swift run`) for performance runs

## 8) Service control

```bash
f kar-uc-launchd-restart
f kar-uc-launchd-stop
f kar-uc-launchd-uninstall
```

If something is wrong after reinstall:

```bash
f kar-uc-launchd-uninstall
f kar-uc-launchd-install
f kar-uc-launchd-status
```
