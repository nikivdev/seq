# Fast Path: OS-Level Completion + Next RL Run

This is the shortest reliable loop for your workflow.

## 1) Bring up always-on capture + prediction

From `~/code/seq`:

```bash
f seq-harbor-install
f seq-harbor-run
f next-type-fast-on
f seq-harbor-status
```

What you get:
- keystroke capture (`next_type.key_*`)
- online predictor suggestions (`next_type.suggestion_emit.v1`)
- one-shot accept command (`f next-type-accept`)
- Kar + agent Q/A + watchdog supervised by launchd

## 2) Tab-complete style acceptance

Use this command directly now:

```bash
f next-type-accept
```

To bind this to a hotkey in Kar config (`/Users/nikiv/config/i/kar/config.ts`), map any key/chord to:

```ts
shell("cd ~/code/seq && f next-type-accept")
```

Recommended: bind on a layer chord (not plain `tab`) to avoid clobbering native app tab behavior.

## 3) Prepare next RL training run in one command

From `~/code/seq`:

```bash
f rl-next-run-prep
```

This runs:
- strict `seq-health`
- router export + audit
- kar export + audit
- high-signal summary including `agent.qa`

Report:

```bash
f rl-next-run-report
```

Default report path:
- `~/.local/state/seq/rl_next_run_prep_report.json`

## 4) Launch training run after gates pass

```bash
cd ~/repos/PrimeIntellect-ai/prime-rl
f router-env-push
f router-run-qwen3-30b-a3b-erl-reflect
f router-early-stop-watch
f router-gate
```

## Latency profile

- Capture path is listen-only + append-only file writes.
- Predictor is local, online, and rate-limited (cooldown).
- Accept path uses direct seq RPC (`type_text`) for low overhead.
