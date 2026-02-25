# seq_everruns_bridge

Reusable Everruns client-side tool bridge for `seqd`.

This crate centralizes:
- Everruns client-side `seq_*` tool definitions
- Tool name -> `seqd` RPC op mapping
- Correlated RPC request shaping (`request_id`, `run_id`, `tool_call_id`)
- Tool-call execution against `seq_client::SeqClient`

## Why

Flow and other agent runtimes can share one implementation instead of duplicating
`tool.call_requested` handling logic.

## Usage

```rust
use seq_client::SeqClient;
use seq_everruns_bridge::{execute_tool_call, ToolCall};
use serde_json::json;
use std::time::Duration;

let client = SeqClient::connect_with_timeout("/tmp/seqd.sock", Duration::from_secs(5))?;
let call = ToolCall {
    id: "tool-1".to_string(),
    name: "seq_open_app".to_string(),
    arguments: json!({"name": "Safari"}),
};

let result = execute_tool_call(&client, "session-abc", "event-123", &call);
println!("{}", serde_json::to_string_pretty(&result)?);
# Ok::<(), Box<dyn std::error::Error>>(())
```

## Flow Integration Sketch

In `f ai everruns`:
1. Use `client_side_tool_definitions()` when creating sessions.
2. Parse `tool.call_requested` payload with `parse_tool_call_requested`.
3. Optionally configure Maple dual-ingest exporter and use `execute_tool_call_with_maple(...)`.
4. Submit returned `ToolResult` values back to `/v1/sessions/{id}/tool-results`.

## Maple Dual Ingest (Local + Hosted)

Best-effort, non-blocking OTLP trace export is available via `maple::MapleTraceExporter`.
Tool calls are emitted as `everruns.tool_call` spans.
Non-tool lifecycle spans can be emitted with `maple::MapleSpan::for_runtime_event(...)`.

```rust
use seq_everruns_bridge::{
    execute_tool_call_with_maple, parse_tool_call_requested,
    maple::MapleTraceExporter,
};

let exporter = MapleTraceExporter::from_env()?;
let calls = parse_tool_call_requested(data)?;
for call in calls {
    let result = execute_tool_call_with_maple(
        &client,
        session_id,
        event_id,
        &call,
        exporter.as_ref(),
    );
    // send result back to Everruns
}
# Ok::<(), Box<dyn std::error::Error>>(())
```

Supported environment variables:

- `SEQ_EVERRUNS_MAPLE_LOCAL_ENDPOINT` (example: `http://ingest.maple.localhost/v1/traces`)
- `SEQ_EVERRUNS_MAPLE_LOCAL_INGEST_KEY`
- `SEQ_EVERRUNS_MAPLE_HOSTED_ENDPOINT` (example: `https://ingest.maple.dev/v1/traces`)
- `SEQ_EVERRUNS_MAPLE_HOSTED_INGEST_KEY`

Or CSV form:

- `SEQ_EVERRUNS_MAPLE_TRACES_ENDPOINTS=url1,url2`
- `SEQ_EVERRUNS_MAPLE_INGEST_KEYS=key1,key2`

Tuning:

- `SEQ_EVERRUNS_MAPLE_QUEUE_CAPACITY` (default `4096`)
- `SEQ_EVERRUNS_MAPLE_MAX_BATCH_SIZE` (default `128`)
- `SEQ_EVERRUNS_MAPLE_FLUSH_INTERVAL_MS` (default `50`)
- `SEQ_EVERRUNS_MAPLE_CONNECT_TIMEOUT_MS` (default `400`)
- `SEQ_EVERRUNS_MAPLE_REQUEST_TIMEOUT_MS` (default `800`)
