# Flow Recipe Authoring And Execution

This guide defines a simple, low-latency recipe workflow that works with `f recipe`, with MoonBit (`.mbt`) as the preferred executable format.

## Goals

- Keep recipes discoverable and executable from Flow.
- Support both shared/global recipes and project-specific recipes.
- Make it easy to ask Codex to generate optimized recipes directly into `.ai/recipes/`.

## Recipe locations

- Project recipes:
  - `.ai/recipes/project/*.md`
- Global recipes:
  - `~/.config/flow/recipes/*.md`
  - For local experimentation only, you can use `.ai/recipes/global/*.md` with `--global-dir`.

## Recipe file format

Each recipe can be either:

1. A MoonBit file (`.mbt`) (preferred):
   - optional metadata in top comments
     - `// title: ...`
     - `// description: ...`
     - `// tags: [a, b]`
2. A markdown recipe (`.md`) with:
   - optional frontmatter (`title`, `description`, `tags`)
   - at least one executable shell fenced block (`sh`, `bash`, `zsh`, `fish`, or empty fence).

Example:

MoonBit example:

```mbt
// title: First MoonBit Recipe
// description: Minimal moonbit recipe
// tags: [moonbit, recipe]

fn main {
  println("moonbit recipe: ok")
}
```

Markdown example (legacy-compatible):
````md
---
title: Open Safari New Tab
description: Fast seq smoke command.
tags: [seq, app]
---

```sh
/Users/nikiv/code/seq/cli/cpp/out/bin/seq run "open Safari new tab"
```
````

For markdown recipes, Flow executes the first shell fence it finds. For `.mbt` recipes, Flow executes `moon run <file>`.

## Flow commands

Initialize directories and starter recipes:

```bash
f recipe init --scope all
```

For repo-local global testing:

```bash
f recipe init --scope all --global-dir .ai/recipes/global
```

List/search:

```bash
f recipe list
f recipe search safari
```

Run:

```bash
f recipe run project:open-safari-new-tab
f recipe run "Open Safari New Tab"
f recipe run "latency bench" --dry-run
```

## Codex prompt template (copy/paste)

Use this prompt when you want Codex to generate an optimized recipe (prefer `.mbt`):

```text
Create one Flow recipe markdown file for this task:
<TASK DESCRIPTION>

Requirements:
- Prefer writing to .ai/recipes/project/<slug>.mbt
- Add metadata comments at top: title, description, tags
- Keep execution path low-latency and deterministic
- Use existing tools/scripts in this repo when possible
- Keep recipe idempotent or clearly safe to rerun
- If `.md` is necessary, explain why `.mbt` is not suitable
- After writing, show:
  1) the file path
  2) exact command to run via Flow: f recipe run <id-or-name>
```

## Optimization rules for recipe commands

- Prefer direct binary invocation over nested wrappers.
- Avoid process chains unless necessary.
- Reuse already-running daemons/services.
- Keep I/O small and local.
- If a task is benchmark-related, include fixed iterations/warmup for reproducibility.

## Recommended lifecycle

1. Ask Codex to create recipe in `.ai/recipes/project/`.
2. Validate with:
   - `f recipe search <keyword>`
   - `f recipe run <recipe> --dry-run`
3. Execute for real with `f recipe run <recipe>`.
4. Promote stable recipes to global set when reused across repos.
