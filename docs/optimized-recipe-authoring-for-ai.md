# Optimized Recipe Authoring For AI

This guide is the context packet to give an AI when you want it to write fast recipes for a project.

It is designed for your current stack:
- Flow recipes (`f recipe`)
- seq/seqd command execution
- Karabiner low-latency paths (`send_user_command` and direct seq sockets)

Use this when your priority is: lowest possible latency without sacrificing reliability.

## 1) Performance model (what actually costs time)

The biggest latency costs are usually:
1. process spawn (`/bin/sh`, `bash -lc`, nested wrappers)
2. unnecessary IPC hops
3. app activation and compositor frame timing (outside your script)

The smallest costs are usually:
1. tiny JSON encode/decode
2. local Unix socket send/recv
3. in-process dispatch

Implication:
- optimize away shell/process churn first
- keep command paths short and direct
- do not micro-optimize codec format before removing spawns

## 2) Golden rules for fast recipes

1. Prefer direct binaries over shell wrappers.
2. Prefer seq native/socket paths over `shell("seq ...")`.
3. Keep exactly one execution path for the hot action.
4. Keep recipe steps minimal and deterministic.
5. Pre-resolve paths at build/author time (avoid runtime lookup chains).
6. Avoid `osascript` in hot paths unless absolutely required.
7. Use idempotent steps so retries are safe.
8. Put expensive setup in separate cold-start recipes, not hot recipes.

## 3) Fast-path hierarchy (choose highest available)

For interactive hot actions (open app, open file in app, paste, enter):

1. Best: typed low-latency command to seqd over Unix socket
2. Good: Karabiner `send_user_command` -> local receiver -> seqd socket
3. Acceptable fallback: direct CLI binary invocation without subshell chains
4. Worst: `bash -lc` with multiple nested tools

## 4) Recipe types and where they belong

Project recipes:
- `.ai/recipes/project/*.md`
- use for repo-specific workflows

Global recipes:
- `~/.config/flow/recipes/*.md`
- use for reusable personal workflows

Action-pack markdown (separate pipeline):
- use `action-pack` blocks when you need signed remote execution
- see `docs/action-pack-recipe-execution.md`

## 5) AI output contract (strict)

When asking AI to generate a recipe, require:

1. Exactly one recipe file path.
2. Frontmatter with `title`, `description`, `tags`.
3. Exactly one executable block (`sh`).
4. No nested shell wrappers unless justified.
5. Explicit fallback behavior, if any.
6. Verification commands.
7. Clear expected result text.

## 6) Prompt template for AI (copy/paste)

```text
Create one optimized Flow recipe for this task:
<TASK>

Constraints:
- Write exactly one file to .ai/recipes/project/<slug>.md
- Include frontmatter: title, description, tags
- Include exactly one executable sh block
- Prioritize lowest-latency path: avoid /bin/sh wrappers and process chains
- Prefer existing seq-native or socket-native commands
- Keep steps deterministic and idempotent
- If fallback is needed, make it explicit and cheap
- Reuse existing repo tools/scripts if available

After writing, output:
1) file path
2) exact run command: f recipe run <id-or-name>
3) expected success output
4) quick benchmark command (p50/p95/p99)
```

## 7) Authoring checklist before running

1. Does this recipe spawn extra shells?
2. Can any step be replaced by direct seq/daemon call?
3. Are all absolute paths stable on this machine?
4. Is the recipe safe to re-run?
5. Is the verification command cheap and clear?

If any answer is no, revise before running.

## 8) Examples

### 8.1 Good (direct path)

````md
---
title: Open Safari New Tab
description: Fast seq-native macro call.
tags: [seq, hotpath]
---

```sh
/Users/nikiv/code/seq/cli/cpp/out/bin/seq run "open Safari new tab"
```
````

Why good:
- direct binary call
- no nested shell
- one action only

### 8.2 Bad (avoid)

````md
```sh
bash -lc 'sh -lc "seq run \"open Safari new tab\""' 
```
````

Why bad:
- unnecessary shell layers
- more spawn overhead
- harder error visibility

## 9) Benchmark protocol for recipe changes

For hot-path recipes, compare old vs new with repeated runs.

Minimum:
1. 20 warmups
2. 100 timed iterations
3. report p50/p95/p99 and failures

Useful existing tools in this repo:
- `tools/kar_user_command_latency_bench.py`
- `tools/kar_user_command_floor_check.py`
- `tools/bench_open_app.py`

Rule:
- keep new recipe only if p95 is not worse and functional behavior matches.

## 10) Reliability guardrails

1. Add clear timeout boundaries to long steps.
2. Emit one-line success markers for automation.
3. Keep side effects explicit (file writes, app launches, network calls).
4. For remote execution, use action-pack policies and signed packs.

## 11) Recipe review rubric

Reject recipe if:
- it introduces extra process layers for hot path
- it relies on fragile UI timing sleeps without reason
- it hides failures (`|| true`) on critical steps
- it lacks a verification step

Approve recipe if:
- path is direct and minimal
- behavior is deterministic
- verification is cheap
- rollback/fallback is clear

## 12) Practical workflow

1. Ask AI with the prompt template above.
2. Run `f recipe run <recipe> --dry-run`.
3. Execute real run.
4. Benchmark if hot-path.
5. Keep only if p95 and correctness are stable.

---

Related docs:
- `docs/flow-recipe-authoring-and-execution.md`
- `docs/executable-md-recipes.md`
- `docs/action-pack-recipe-execution.md`
- `docs/karabiner-user-command-absolute-minimum.md`
