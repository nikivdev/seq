# Full Agent Session Ingest + RAG Plan (Claude + Codex -> ClickHouse -> Kreuzberg)

This is the implementation plan to extend the current `seq` ClickHouse observability so we store **full agent session content** (not just token/tool telemetry), and then build an effective local RAG layer on top using `~/repos/kreuzberg-dev/kreuzberg`.

## Implementation Status (February 13, 2026)

Implemented now:
- `agent.raw_lines` and `agent.messages` are live and being written by the ingest daemon.
- `agent.sessions`, `agent.turns`, and `agent.tool_calls` now also write through HTTP (shared-host friendly), with optional native dual-write via `SEQ_CH_ENABLE_NATIVE_WRITES=1`.
- `rag.documents` and `rag.chunks` tables are live in ClickHouse.
- `seqch` now supports:
  - `seqch rag index ...` to build/update the retrieval corpus from `agent.messages`
  - `seqch rag search "<query>" ...` for retrieval over indexed chunks
  - `seqch rag stats` for corpus health

Not implemented yet (next phase):
- Kreuzberg-powered embeddings/chunking pipeline for semantic retrieval.
- Hybrid lexical + vector ranking in `seqch rag search`.

## Current State (Already Implemented)

- ClickHouse tables:
  - `seq.context` (Mac app/window/URL/AFK)
  - `hive.*` (supersteps, model_invocations, tool_calls)
  - `agent.sessions`, `agent.turns`, `agent.tool_calls` (telemetry from JSONL ingest)
- Ingest:
  - Swift daemon in Hive repo tails `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/*.jsonl`
  - Currently captures: turns + tool calls + session aggregates
- Query CLI:
  - `seqch` (in `~/code/org/linsa/base`) for querying these tables

## Goal

1. Persist **complete transcripts**: every user/assistant/developer message, reasoning blocks (where present), tool_use + tool_result.
2. Persist enough metadata to filter quickly (repo path, branch, file paths touched, tool name, error/success).
3. Build RAG retrieval that is:
   - fast locally
   - filterable (by repo, time window, agent, tool)
   - good at “find that thing I did last week” across Claude and Codex

## Phase A: Store All Session Content in ClickHouse

### A1. Add Raw JSONL Line Table (Lossless)

Store the exact original JSON line for replay/debugging.

**DB:** `agent`

**Table:** `agent.raw_lines`
- `ts_ms UInt64`
- `agent LowCardinality(String)` ('claude' | 'codex')
- `session_id String`
- `source_path String`
- `source_offset UInt64` (byte offset, monotonic per file)
- `line_type LowCardinality(String)` (best-effort extracted)
- `json String` (the full line)

Engine:
- `MergeTree`
- `PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))`
- `ORDER BY (agent, session_id, ts_ms, source_offset)`
- Optional TTL: 30-180 days (this can get big)

Why:
- no schema churn when upstream formats evolve
- lets you re-parse into new normalized tables later

### A2. Add Normalized Message Table (For Query + RAG)

**Table:** `agent.messages`
- `ts_ms UInt64`
- `agent LowCardinality(String)`
- `session_id String`
- `message_id String` (Claude: `uuid` or `message.id`; Codex: derive from file + line no/offset)
- `role LowCardinality(String)` ('user' | 'assistant' | 'developer' | 'system')
- `kind LowCardinality(String)` ('text' | 'reasoning' | 'tool_use' | 'tool_result' | 'progress' | 'snapshot')
- `model LowCardinality(String)` (when known)
- `project_path String` (repo/cwd; best effort)
- `git_branch String DEFAULT ''`
- `tool_name LowCardinality(String) DEFAULT ''`
- `tool_call_id String DEFAULT ''` (Claude: tool_use id; Codex: call_id)
- `ok UInt8 DEFAULT 1`
- `text String` (human-readable extracted text; for RAG)
- `json String` (optional: raw message payload; expensive but useful)

Engine:
- `MergeTree`
- `PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))`
- `ORDER BY (agent, project_path, session_id, ts_ms)`

### A3. Extend Ingest Daemon

Update the Swift ingest daemon to:
1. always write each JSONL line into `agent.raw_lines` (lossless)
2. also parse and write derived rows into `agent.messages`

Parsing notes:
- **Codex**: `response_item.payload.type == "message"` contains message text in `content[]` items:
  - `input_text.text` (system/developer/user)
  - `output_text.text` (assistant)
- **Codex**: `event_msg.payload.type == "agent_reasoning"` includes a text blob (store as `kind=reasoning`)
- **Claude**:
  - `type=assistant` with `message.content[]` items:
    - `type=text` content blocks
    - `type=tool_use` blocks (store tool name + input summary)
  - `type=user` tool results arrive as `content[]` blocks `type=tool_result` (store ok/is_error + output text)

Insertion options:
- **Preferred for big strings**: ClickHouse HTTP `INSERT INTO ... FORMAT JSONEachRow` directly from the daemon (no new C ABI needed).
- Alternate: extend `libseqch.dylib` with new push fns for `agent.messages` and `agent.raw_lines` (more work; large-string bridge).

### A4. Project Association (Make It Reliable)

Right now project path comes from:
- Claude: `cwd` field on line (often present)
- Codex: `turn_context.payload.cwd` (present)

To make it reliable:
- store both `cwd` and `repo_root` (resolve by walking up to `.git` if available)
- store `repo_id` as a stable hash of repo root path (useful for joins + compact)

## Phase B: Prepare Data For Retrieval (Chunking + Embeddings)

We want a “retrieval corpus” table that is stable, chunked, and embed-able.

### B1. RAG Tables

**DB:** `rag` (new)

`rag.documents` (one “document” per message/tool-output/snapshot)
- `doc_id String` (hash)
- `ts_ms UInt64`
- `agent LowCardinality(String)`
- `session_id String`
- `project_path String`
- `doc_type LowCardinality(String)` ('message' | 'tool_result' | 'file_snapshot' | 'reasoning')
- `title String`
- `text String`
- `meta_json String`

`rag.chunks`
- `chunk_id String`
- `doc_id String`
- `ts_ms UInt64`
- `project_path String`
- `text String`
- `keywords Array(String)` (optional)
- `embedding Array(Float32)` (or store separately)

Engine:
- `MergeTree`
- `PARTITION BY toYYYYMMDD(toDateTime(ts_ms / 1000))`
- `ORDER BY (project_path, ts_ms, doc_id, chunk_id)`

### B2. Use Kreuzberg For Chunking + Embeddings

Kreuzberg already has:
- chunking (`max_chars`, overlap)
- embeddings (ONNX or preset models)
- keyword extraction

Integration options:
- use Kreuzberg as a CLI tool invoked by the indexer
- or use its Rust crate directly from a Rust indexer
- or run Kreuzberg as a local server/MCP and call it

First version:
- build a local indexer job that:
  1. reads new `agent.messages` rows since last watermark
  2. maps each row to a `rag.document`
  3. calls Kreuzberg chunk+embed
  4. writes chunks back to ClickHouse

### B3. Concrete Kreuzberg Embedding Rollout (Next)

1. Create `rag.chunk_embeddings`:
   - `chunk_id String`
   - `model LowCardinality(String)` (e.g. `bge-small-en-v1.5`)
   - `embedding Array(Float32)`
   - `dim UInt16`
   - `ts_ms UInt64`

2. Add a watermark table:
   - `rag.index_state (name String, value String, updated_at DateTime)`
   - Keep one row per indexer (`agent_messages_embedding`).

3. Add indexer executable (Swift or Rust):
   - Read candidate chunks from `rag.chunks` where `chunk_id` not in `rag.chunk_embeddings`.
   - Batch 64-256 chunks per request.
   - Call Kreuzberg (Python or CLI wrapper) to generate embeddings.
   - Insert embeddings into `rag.chunk_embeddings`.

4. Add retrieval query mode:
   - lexical candidate prefilter (`positionCaseInsensitive` / ngram distance) to top N (e.g. 500)
   - vector rerank with cosine distance against `rag.chunk_embeddings`
   - return top K with `project_path`, `session_id`, `chunk_text`.

5. Add CLI options:
   - `seqch rag search "<q>" --mode lexical|hybrid|vector`
   - default to `hybrid` once embeddings are available.

## Phase C: Retrieval API + CLI

### C1. Basic Search (No Fancy Index)

For personal-scale data, brute force with filtering is fine:

1. generate embedding for query (Kreuzberg)
2. ClickHouse query:
   - filter by `project_path` (and/or time)
   - compute cosine distance over `embedding Array(Float32)`
   - return top K chunks + surrounding context (doc_id -> doc text)

This will work well until chunk counts get into the high hundreds of thousands.

### C2. Improve UX: “Pre-flight Brief” + “Handoff Note”

Use retrieval to automatically produce:
- the top 10 “most relevant prior chunks” for the current repo + task
- plus “recent failures” and “recent sessions” (from `agent.*` MVs)

Write these to:
- Claude: `~/.claude/projects/<encoded>/memory/agent-context.md`
- Codex: repo-local `docs/agent-context.md` referenced by `AGENTS.md`

### C3. Correlate With Mac Context (Seq)

Use `seq.context` timestamps to add filters like:
- "while I was in Xcode"
- "while I was on Arc reading docs"
- "not AFK"

Implementation:
- an `ASOF JOIN` between `agent.messages.ts_ms` and `seq.context.ts_ms` to annotate each message with active app/window/url.

## Phase D: Operational Concerns

- Size control:
  - TTL for `agent.raw_lines` (keep 30-90 days)
  - keep `agent.messages.text` indefinitely (or longer TTL)
  - keep embeddings indefinitely (they are compact)
- Privacy:
  - optional redaction pass for secrets (regex-based) before writing to `agent.messages`/`rag.*`
- Backfill:
  - do it once per agent source, with a separate state file so the daemon can stay “live only”
