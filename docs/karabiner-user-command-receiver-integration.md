# Karabiner User-Command Receiver Integration (seq)

This guide explains how to test Karabiner's upcoming `send_user_command` integration on top of seq **without breaking the current `seqSocket(...)` path**.

## Compatibility goals

1. Keep existing path intact:
   - Karabiner `socket_command` -> `/tmp/seqd.sock` / `/tmp/seqd.sock.dgram`
2. Add new path as optional:
   - Karabiner `send_user_command` JSON -> receiver socket -> bridge -> existing seqd command protocol
3. No required changes to current `/Users/nikiv/config/i/kar/config.ts` mappings during pilot.

## Components

- seq daemon (existing): `/tmp/seqd.sock` and `/tmp/seqd.sock.dgram`
- Karabiner receiver repo:
  - `~/repos/pqrs-org/Karabiner-Elements-user-command-receiver`
  - executable: `seq-user-command-bridge`
- bridge behavior:
  - receives JSON payloads on `~/.local/share/karabiner/tmp/karabiner_user_command_receiver.sock`
  - maps to seqd lines:
    - `{"v":1,"type":"run","name":"X"}` -> `RUN X`
    - `{"v":1,"type":"open_app_toggle","app":"Safari"}` -> `OPEN_APP_TOGGLE Safari`
  - forwards dgram-first, stream fallback.

## Flow tasks added in seq

- `f kar-uc-build-bridge`
- `f kar-uc-run-bridge`
- `f kar-uc-send`
- `f kar-uc-smoke`

## What this setup gives you

- A deterministic smoke test that does not require Karabiner key mappings.
- A real-path sender for live bridge testing.
- A maintainer-friendly path to validate new `send_user_command` integration on top of seq.
- Full backward compatibility with existing `seqSocket(...)` transport.

## Test plan for Karabiner maintainer

### 1) Fast protocol smoke (no Karabiner needed)

Runs bridge + mock seqd listener, validates exact forwarding line.

```bash
cd ~/code/seq
f kar-uc-smoke
```

Expected:

- `ok: bridge forwarded expected command`
- forwarded command line is `RUN open Safari new tab`

### 2) Real seqd + bridge test (no Karabiner key mapping needed)

Terminal A:

```bash
cd ~/code/seq
f deploy
f kar-uc-run-bridge
```

Terminal B:

```bash
cd ~/code/seq
./cli/cpp/out/bin/seq ping
f kar-uc-send --run "open Safari new tab"
```

Expected:

- `seq ping` returns `PONG`
- bridge logs forwarding activity
- seq executes the macro as if it came from legacy `seqSocket(...)`.

### Maintainer quick path

```bash
cd ~/code/seq
f kar-uc-smoke
f deploy
f kar-uc-run-bridge
```

In another terminal:

```bash
cd ~/code/seq
f kar-uc-send --run "open Safari new tab"
```

Expected:

- smoke returns `ok: bridge forwarded expected command`
- bridge logs forwarding line
- seqd executes the requested macro

### 3) Karabiner pilot mapping test (when maintainer wire-up is ready)

Use pilot snippets from:

- `~/repos/pqrs-org/Karabiner-Elements-user-command-receiver/docs/karabiner/`

Suggested rollout:

1. Keep legacy mode default.
2. Enable user-command mode for 5 pilot keys only.
3. Compare success rate and latency p95/p99.
4. Revert pilot mode immediately if failures appear.

## Rollback

Rollback is immediate and safe:

- stop bridge process
- keep/use legacy `seqSocket(...)` mappings only

No seq daemon protocol change is required for rollback.

## Notes

- This integration is additive. It does not replace seqd socket protocol.
- If Karabiner final `send_user_command` wrapper shape changes, only the Karabiner-side wrapper payload needs adjustment; seq bridge payload mapping remains stable.

## Local validation commands (used while preparing this doc)

```bash
python3 -m py_compile tools/kar_user_command_send.py tools/kar_user_command_smoke.py
python3 tools/kar_user_command_smoke.py --timeout-s 4
f kar-uc-smoke --timeout-s 4
```
