# Flow <-> Seq <-> Everruns Integration (Submodule-Ready)

This document describes a low-risk integration path so Flow can drive Everruns while reusing Seq's canonical tool bridge.

## Goal

Keep one source of truth for Everruns client-side seq tools:

- tool catalog (`seq_*` definitions)
- tool-name normalization
- correlation IDs for RPC (`request_id`, `run_id`, `tool_call_id`)
- tool execution against `seqd`

## Shared Bridge

Use:

- `api/rust/seq_everruns_bridge` (this repo)

It builds on:

- `api/rust/seq_client`

## Recommended Wiring in Flow

1. Add Seq as a submodule in Flow (example path: `third_party/seq`).
2. Add path deps in Flow `Cargo.toml`:

```toml
[dependencies]
seq_client = { path = "third_party/seq/api/rust/seq_client" }
seq_everruns_bridge = { path = "third_party/seq/api/rust/seq_everruns_bridge" }
```

3. In Flow `ai_everruns` runtime:
- session create: call `seq_everruns_bridge::client_side_tool_definitions()`
- `tool.call_requested`: parse with `parse_tool_call_requested`
- execute each call with `execute_tool_call_with_maple(...)` (or `execute_tool_call(...)` when disabled)
- forward returned `ToolResult` payload to Everruns `/tool-results`

## Why This Is Better

- avoids drift between Flow and Seq tool mappings
- keeps protocol edge cases in one tested crate
- allows independent iteration in Seq without hand-copying logic into Flow

## Migration Pattern

1. Introduce bridge crate usage behind a Flow feature flag (optional).
2. Validate one end-to-end session with `seqd` running.
3. Remove duplicated mapping/tool-definition code in Flow once stable.

## Maple Trace Export (Local + Hosted)

`seq_everruns_bridge` can dual-write tool-call traces to Maple OTLP ingest endpoints without blocking the tool path:

- local Maple (for fast dev visualization)
- hosted Maple (`ingest.1focus.ai`) for shared history

Flow runtime sketch:

```rust
let maple = seq_everruns_bridge::maple::MapleTraceExporter::from_env()?;
for call in calls {
    let result = seq_everruns_bridge::execute_tool_call_with_maple(
        &seq_client,
        &session_id,
        &event_id,
        &call,
        maple.as_ref(),
    );
    // push result to Everruns
}
```

To feed full runtime telemetry (not only tool calls), emit additional spans for SSE/session stages:

```rust
if let Some(maple) = maple.as_ref() {
    maple.emit_span(seq_everruns_bridge::maple::MapleSpan::for_runtime_event(
        &session_id,
        &event_id,
        "sse_batch",
        true,
        None,
        start_ns,
        end_ns,
        vec![("events.count".to_string(), events_count.to_string())],
    ));
}
```

Suggested Flow env keys:

- `SEQ_EVERRUNS_MAPLE_LOCAL_ENDPOINT`
- `SEQ_EVERRUNS_MAPLE_LOCAL_INGEST_KEY`
- `SEQ_EVERRUNS_MAPLE_HOSTED_ENDPOINT`
- `SEQ_EVERRUNS_MAPLE_HOSTED_INGEST_KEY`

Use Flow env store for secrets:

```bash
f env set SEQ_EVERRUNS_MAPLE_LOCAL_INGEST_KEY=maple_pk_xxx
f env set SEQ_EVERRUNS_MAPLE_HOSTED_INGEST_KEY=maple_pk_xxx
```

For optimized local durability + remote analytics, also set:

```bash
f ch-mode mirror --host <your-remote-clickhouse-host> --port 9000 --database seq
```
