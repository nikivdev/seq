# Running seq with Karabiner Elements

Guide for setting up [seq](https://github.com/nikivdev/seq) (macro engine) + [kar](https://github.com/nikivdev/kar) (Karabiner config generator) + [Karabiner Elements](https://github.com/pqrs-org/Karabiner-Elements) on a fresh Mac.

## Overview

The system has three pieces:

1. **Karabiner Elements** - listens for keyboard events, triggers actions
2. **kar** - transpiles a TypeScript config into Karabiner's JSON format
3. **seq** - C++ macro engine daemon (`seqd`) that executes macros triggered by Karabiner

Data flow:

```
config.ts  ──kar build──▶  karabiner.json  ──Karabiner──▶  /tmp/seqd.sock  ──seqd──▶  action
```

When you press a key combo, Karabiner either:
- Sends a `socket_command` directly to the seq daemon via Unix socket (fast, ~100ms)
- Spawns a shell command that calls the `seq` CLI binary (fallback, ~200ms)

## Prerequisites

- macOS 14+
- [Karabiner Elements](https://karabiner-elements.pqrs.org/) installed
- Rust toolchain (`cargo`) - for building kar
- Xcode Command Line Tools (`xcode-select --install`) - for building seq
- Python 3 - for seq macro generation
- [flow](https://github.com/nikivdev/flow) (`f` CLI) - build/deploy tool

### Install flow

```bash
# See https://github.com/nikivdev/flow for latest install instructions
# After install, `f` should be in your PATH
```

## 1. Clone repos

```bash
mkdir -p ~/code
git clone https://github.com/nikivdev/kar.git ~/code/kar
git clone https://github.com/nikivdev/seq.git ~/code/seq
```

## 2. Set up kar (Karabiner config generator)

```bash
cd ~/code/kar

# Install Rust dependencies and build kar binary → ~/bin/kar
f deploy
```

This puts `kar` in `~/bin/kar`. Make sure `~/bin` is in your PATH:

```bash
export PATH="$HOME/bin:$PATH"  # add to your .zshrc
```

### Where the config lives

kar reads its config from `~/.config/kar/config.ts`. The types directory is installed automatically by `f deploy` to `~/.config/kar/types/`.

Create (or symlink) your config:

```bash
mkdir -p ~/.config/kar

# Option A: start from an example
cp ~/code/kar/examples/simple/config.ts ~/.config/kar/config.ts

# Option B: use the full config from nikiv's dotfiles
# (requires cloning https://github.com/nikivdev/config)
git clone https://github.com/nikivdev/config.git ~/config
ln -sf ~/config/i/kar ~/.config/kar
```

If using nikiv's config directly, `~/.config/kar/` becomes a symlink to `~/config/i/kar/` which contains:
- `config.ts` - the main Karabiner config (~2300 lines of key mappings)
- `types/index.ts` - TypeScript types + helper functions (`openApp`, `seqSocket`, `km`, etc.)

### Build and apply the Karabiner config

```bash
kar              # build config.ts → writes to ~/.config/karabiner/karabiner.json
kar watch        # watch for changes and rebuild automatically
kar --dry-run    # print JSON without writing (good for testing)
```

kar creates/updates a Karabiner profile named `kar`. Switch to this profile in Karabiner Elements preferences.

## 3. Set up seq (macro engine)

```bash
cd ~/code/seq

# Build seq binary + seqmem dylib + restart daemon
f deploy
```

This does:
1. Runs `tools/gen_macros.py` to generate `seq.macros.yaml` from your kar config
2. Builds the Swift `libseqmem.dylib` (memory engine)
3. Compiles the C++/ObjC++ binary → `cli/cpp/out/bin/seq`
4. Codesigns everything
5. Restarts `seqd` daemon via flow

The seq binary lives at `~/code/seq/cli/cpp/out/bin/seq`.

### Verify seq is running

```bash
# Ping the daemon
~/code/seq/cli/cpp/out/bin/seq ping

# Check daemon perf stats (includes ax_trusted status)
~/code/seq/cli/cpp/out/bin/seq perf

# List available macros
~/code/seq/cli/cpp/out/bin/seq help
```

### Grant macOS permissions

seqd needs these permissions in **System Settings > Privacy & Security**:

| Permission | Why | Grant to |
|---|---|---|
| **Accessibility** | Window queries (AX API), keystroke injection, Cmd-Tab | `seq` binary and/or `SeqDaemon.app` |
| **Screen Recording** | ScreenCaptureKit for screen capture/OCR | `seq` binary directly |
| **Input Monitoring** | CGEventTap for AFK detection | `seq` binary and/or `SeqDaemon.app` |

Trigger the Accessibility permission prompt:

```bash
~/code/seq/cli/cpp/out/bin/seq accessibility-prompt
```

## 4. How config.ts connects to seq

The key bridge is the helper functions in `types/index.ts`:

### `seqSocket(macroName)` - fast path via Unix socket

```typescript
// In types/index.ts
export function seqSocket(macroName: string, endpoint = "/tmp/seqd.sock") {
  return { socket_command: { endpoint, command: `RUN ${macroName}` } }
}
```

When kar builds the config, this becomes a `socket_command` in karabiner.json:

```json
{
  "to": [{
    "socket_command": {
      "endpoint": "/tmp/seqd.sock",
      "command": "RUN open-app-toggle:Arc"
    }
  }]
}
```

Karabiner connects to `/tmp/seqd.sock` and sends `RUN open-app-toggle:Arc\n`. seqd receives it and executes the macro instantly. No shell fork, no process spawn.

> **Note**: `socket_command` requires Karabiner Elements with socket support (see [PR #4396](https://github.com/pqrs-org/Karabiner-Elements/pull/4396)). Without it, use `openApp()` or `shell()` which fork a `/bin/sh` process.

### `openApp(name)` - shell fallback

```typescript
export function openApp(app: string) {
  // Tries seq CLI first, falls back to `open -a`
  return shell(`if [ -x "${seqBin}" ]; then "${seqBin}" open-app-toggle "Arc" && exit 0; fi; open -a "Arc"`)
}
```

This works without socket support but is slower (~200ms vs ~100ms) because Karabiner has to fork `/bin/sh`.

### `seq(macroName, steps)` - multi-step sequences

```typescript
// Opens Arc, then sends keystrokes to switch to a specific tab
seq("X Feed (in Arc)", [openApp("Arc"), keystroke("ctrl+1"), keystroke("cmd+6")])
```

This generates a macro in `seq.macros.yaml` that seqd executes step-by-step.

## 5. Macro generation

When you run `f deploy` in the seq project (or `f build`), it runs `tools/gen_macros.py` which:

1. Reads `~/.config/kar/config.ts` (your Karabiner TypeScript config)
2. Extracts all `seqSocket()`, `seq()`, and `openApp()` calls
3. Resolves URL aliases from TSV files (arc, telegram, web)
4. Writes `seq.macros.yaml` — the macro registry seqd loads at startup

You can also define custom macros in `seq.macros.local.yaml` (not auto-generated, not overwritten).

## 6. Example: adding a new key mapping

Say you want `d + t` to open Terminal:

Edit `~/.config/kar/config.ts`:

```typescript
{
  description: "dkey (apps)",
  layer: "d-mode",
  mappings: [
    { from: "t", to: openApp("Terminal") },
    // ...
  ],
}
```

Then rebuild:

```bash
kar                           # regenerate karabiner.json
cd ~/code/seq && f deploy     # regenerate macros + restart seqd
```

Now pressing `d` + `t` simultaneously will toggle Terminal.

## 7. Testing without Karabiner

You can run macros directly via the seq CLI:

```bash
# Run a named macro
~/code/seq/cli/cpp/out/bin/seq run "open-app-toggle:Terminal"

# Toggle an app
~/code/seq/cli/cpp/out/bin/seq open-app-toggle Terminal

# Send a socket command manually
printf 'RUN open-app-toggle:Terminal\n' | nc -U /tmp/seqd.sock
```

## 8. Troubleshooting

### seqd not responding

```bash
# Check if seqd is running
pgrep -f seqd

# Restart it
cd ~/code/seq && f daemon-restart

# Or full rebuild + restart
cd ~/code/seq && f deploy
```

### Macros not found

```bash
# Regenerate macros from kar config
cd ~/code/seq && f deploy

# Check generated macros
cat ~/code/seq/seq.macros.yaml | head -50
```

### Accessibility not working

```bash
# Check trust status
~/code/seq/cli/cpp/out/bin/seq perf  # look for "ax_trusted": true/false

# Re-trigger permission prompt
~/code/seq/cli/cpp/out/bin/seq accessibility-prompt
```

### Logs

```bash
# Tail all seq logs (human readable)
cd ~/code/seq && f tail-logs

# Follow logs live
cd ~/code/seq && f watch-logs
```

## File locations summary

| Path | What |
|---|---|
| `~/code/kar/` | kar source (Rust) |
| `~/code/seq/` | seq source (C++/ObjC++) |
| `~/.config/kar/config.ts` | Karabiner TypeScript config (input) |
| `~/.config/kar/types/` | TypeScript types + helpers |
| `~/.config/karabiner/karabiner.json` | Generated Karabiner JSON (output) |
| `~/code/seq/seq.macros.yaml` | Auto-generated macro registry |
| `~/code/seq/seq.macros.local.yaml` | Custom macro overrides (optional) |
| `~/code/seq/cli/cpp/out/bin/seq` | seq CLI binary |
| `/tmp/seqd.sock` | Unix socket for seqd communication |
| `~/bin/kar` | kar CLI binary |
