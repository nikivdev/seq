# Everruns -> Maple Dual Ingest

This setup sends Everruns tool-call telemetry from `seq_everruns_bridge` to:

- local Maple (`ingest.maple.localhost`) for fast iteration
- hosted Maple (`ingest.maple.dev`) for shared visualization/history

## 1. Configure Endpoints and Keys

Use Flow env store for keys (recommended):

```bash
f env set SEQ_EVERRUNS_MAPLE_LOCAL_ENDPOINT=http://ingest.maple.localhost/v1/traces
f env set SEQ_EVERRUNS_MAPLE_LOCAL_INGEST_KEY=maple_pk_local_xxx
f env set SEQ_EVERRUNS_MAPLE_HOSTED_ENDPOINT=https://ingest.maple.dev/v1/traces
f env set SEQ_EVERRUNS_MAPLE_HOSTED_INGEST_KEY=maple_pk_hosted_xxx
```

Runtime tuning (optional):

```bash
f env set SEQ_EVERRUNS_MAPLE_QUEUE_CAPACITY=4096
f env set SEQ_EVERRUNS_MAPLE_MAX_BATCH_SIZE=128
f env set SEQ_EVERRUNS_MAPLE_FLUSH_INTERVAL_MS=50
```

## 1.5 Optimized Mirror Mode (Recommended)

For seq's native ClickHouse writers, use mirror mode so local files remain durable while remote ingest runs:

```bash
f ch-mode mirror --host <your-remote-clickhouse-host> --port 9000 --database seq
f ch-mode status
```

This keeps `MEM_TAIL` and local recovery behavior intact while still streaming to remote ClickHouse.

Detailed wrapper reference:

- `docs/clickhouse-mode-wrapper.md`

## 1.6 Forward `seq_mem` + `seq_trace` to Hosted Maple

To export all local seq file-mode telemetry (not only Everruns tool spans), run the forwarder:

```bash
f env set SEQ_MAPLE_FORWARDER_ENDPOINT=https://ingest.maple.dev/v1/traces
f env set SEQ_MAPLE_FORWARDER_INGEST_KEY=maple_pk_hosted_xxx
f maple-forwarder-preflight
f maple-forwarder-on
```

When using launchd capture stack, the forwarder is included via:

```bash
f seq-harbor-run
```

## 2. Wire Export in Everruns Runtime

In the runtime loop, build exporter once and pass it to tool execution:

```rust
let maple = seq_everruns_bridge::maple::MapleTraceExporter::from_env()?;
let result = seq_everruns_bridge::execute_tool_call_with_maple(
    &seq_client,
    &session_id,
    &event_id,
    &call,
    maple.as_ref(),
);
```

For non-tool lifecycle telemetry (SSE receive, tool-results post, completion polling), emit explicit runtime spans:

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

## 3. Validate

1. Run one Everruns session with at least one `seq_*` tool call.
2. Open Maple local traces UI and confirm `everruns.tool_call` spans appear.
3. Open hosted Maple traces UI and confirm same trace IDs appear.

## Notes

- Export is best-effort and non-blocking (bounded queue + background flush).
- If sinks are unavailable, tool-call execution continues unaffected.
- Span IDs are stable-format OTLP hex (`traceId`: 32 chars, `spanId`: 16 chars).
