# Executable Markdown Recipes In `docs/`

This doc clarifies what is executable vs reference-only in `~/code/seq/docs`.

## Current Executable Recipe Surface

As of now, only `action-pack` fenced blocks are executable by the seq recipe pipeline.

Authoritative spec:
- `docs/action-pack-recipe-execution.md`

Execution flow:
1. Caller extracts ` ```action-pack ` block from a markdown file.
2. Caller writes extracted content to a plain `.ap` script.
3. Caller runs `seq action-pack run ...` (or `pack` + `send`).

## What Is Not Auto-Executable

Other fenced blocks in docs are runnable examples, but are not consumed automatically by the action-pack pipeline:
- `bash` blocks
- `sql` blocks
- language snippets (`swift`, etc.)

Treat those as manual runbooks unless a separate runner is explicitly wired.

## Recommended Authoring Rules For Executable Recipes

- Keep exactly one `action-pack` block per recipe file.
- Make command paths absolute for strict receivers.
- Keep hot-path recipes to minimal steps.
- Prefer `seq action-pack run` for one-shot execution.
- Use short TTL values for replay safety.

## Quick Audit Command

Use this to find executable recipe blocks under docs:

```bash
rg -n '^```action-pack$' ~/code/seq/docs/*.md
```
