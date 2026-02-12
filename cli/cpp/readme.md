# seq CLI (C++)

Efficient IO CLI scaffold with trace-first logging.

## Run

- Init (once): `rise cli cpp`
- Build: `rise cli cpp build`
- Build + run (default help): `rise cli cpp run`
- Deploy (build + restart seqd): `rise cli cpp deploy`
- Direct build: `sh ./run.sh`
- Start daemon: `sh ./run.sh daemon`
- Ping daemon: `./out/bin/seq ping`
- Run macro: `./out/bin/seq run "open: Safari"`

Logs go to `out/logs` (or `$RISE_LOG_DIR`).

## Accessibility (macOS)

Anything that sends keystrokes, presses menu items, or does Cmd-Tab style automation needs
Accessibility permission.

Two things to know:

1. `seq run ...` is **local-first** (it runs in the hotkey-launched process), which often works even
   when the always-on daemon cannot.
2. Socket macros (`seqSocket(...)` in kar config) run inside `seqd` and require `seqd` to be
   Accessibility-trusted.

Debug:

- `./out/bin/seq perf` includes `"ax_trusted": true|false` for the daemon.
- `./out/bin/seq accessibility-prompt` triggers the system prompt and reports `local=... seqd=...`.

## Context Capture

seqd continuously captures what you're doing on your Mac:

**Layer 1 — Window context** (`ctx.window` events)
- Polls frontmost app every 1s via Accessibility (AX) APIs
- Records app name, window title, bundle ID, URL (browsers), file path (editors)
- Heartbeat: same context extends duration; change finalizes old event + starts new
- Checkpoint every 30s so data isn't lost on crash
- Query: `printf 'CTX_TAIL 20\n' | nc -U /tmp/seqd.sock`

**Layer 2 — Screen frames** (`ctx.frame` events)
- ScreenCaptureKit at 2 FPS with perceptual hash dedup (8x9 luma grid, Hamming distance 8)
- Adaptive sampling: 2s base interval, doubles when screen is stable, caps at 120s
- HEIC encoding at quality 0.7 via ImageIO (GPU-accelerated)
- Vision OCR on each unique frame, stored in SQLite FTS5
- Local spool: `~/Library/Application Support/seq/frames_spool/` (200MB cap)
- Background SFTP sync to Hetzner storage box every 5 minutes
- Thermal-aware: pauses at Critical, doubles intervals at Serious
- Requires Screen Recording permission for SeqDaemon.app
- Query: `printf 'CTX_SEARCH meeting notes\n' | nc -U /tmp/seqd.sock`

**Layer 3 — AFK detection** (`afk.start` / `afk.end` events)
- CGEventTap (listen-only) monitors keyboard + mouse timestamps
- Never logs content — only "last input at" timestamps
- AFK after 5 minutes of inactivity
- Query: `printf 'AFK_STATUS\n' | nc -U /tmp/seqd.sock`

### Storage budget

| Layer | Stored locally | Stored on Hetzner |
|-------|---------------|-------------------|
| Window context | Wax (<1 MB/day) | — |
| Screen frames (metadata) | Wax (<1 MB/day) | — |
| Screen frames (HEIC) | Spool (200MB cap, synced) | All frames |
| OCR text | SQLite FTS5 (~10-20 MB/day) | — |
| AFK events | Wax (<100 KB/day) | — |

### Hetzner sync

`tools/sync_frames.sh` uploads frames from the local spool to `u533855.your-storagebox.de:/seq/frames/`.
Requires SSH key on the storage box (set up via `infra host storage-box-setup` or manually).
seqd spawns it every 5 minutes. After successful upload, local files are deleted.

### Permissions

seqd (via SeqDaemon.app) needs:
- **Accessibility**: for AX queries (window title, URL) and CGEventTap (AFK)
- **Screen Recording**: for ScreenCaptureKit (screen frames)
- **Input Monitoring**: for CGEventTap (AFK timestamps)

Grant in System Settings > Privacy & Security. The Developer ID signature ensures grants survive rebuilds.

## Layout

- src/main.cpp    entrypoint + commands
- src/io.*        fast buffered output
- src/trace.*     lightweight tracing to files
- src/context.*   window context (AX) + AFK detection (CGEventTap)
- src/capture.*   screen capture (ScreenCaptureKit + OCR + FTS5)
- src/metrics.*   seqmem integration (Wax persistence)
