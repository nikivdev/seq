# mac-knowledge

Local macOS knowledge index for AI queries over files, metadata, and watcher links.

## Goals

- Keep file metadata/content searchable at low latency.
- Track "who watches/depends on this file" for high-value config files.
- Support fast incremental refresh for local development workflows.
- Allow optional vector retrieval through the existing `zvec-server`.

## Commands

```bash
cd ~/code/seq

# Build/refresh index (defaults: ~/config, ~/code/seq, ~/code/org/la/la, ~/code/myflow)
f mac-kg-index

# Focused index for kar config + seq only
f mac-kg-index --root ~/config/i/kar --root ~/code/seq

# Search (lexical)
f mac-kg-search "karabiner open app toggle"

# Search with vector merge (requires zvec-server running)
f mac-kg-search "who handles open app toggle" --vector

# Watch inference for one file
f mac-kg-who-watches /Users/nikiv/config/i/kar/config.ts --refresh

# Stats
f mac-kg-stats
```

## Data location

- SQLite DB: `~/.local/share/seq/mac_knowledge.db`
- Tables:
  - `files` (metadata)
  - `file_content` (title + text preview)
  - `file_fts` (FTS5 index)
  - `watcher_links` (inferred watcher/dependency links)

## Optional vector indexing

If `zvec-server` is running:

```bash
f zvec-server
f mac-kg-index --zvec-url http://127.0.0.1:8900
f mac-kg-search "How is kar config watched?" --vector
```

Notes:

- Current vectorization uses deterministic hashed embeddings for speed/no external model dependency.
- This can be swapped to model embeddings later without changing command surface.

## Current watcher inference sources

- `~/code/seq/flow.toml` tasks
- `~/code/seq/tools/*`
- `~/code/seq/docs/*`
- `~/config/i/kar/types/*`

For `/Users/nikiv/config/i/kar/config.ts`, built-in high-confidence links include:

- `kar watch`
- `kar build`
- `tools/gen_macros.py`
- Karabiner JSON output pipeline
