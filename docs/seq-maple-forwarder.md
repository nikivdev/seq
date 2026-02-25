# Seq Maple Forwarder

Use `tools/seq_maple_forwarder.py` to stream local seq JSONL telemetry to hosted Maple OTLP ingest.

## What It Forwards

- `${SEQ_CH_MEM_PATH}` (`seq_mem.jsonl`) rows
- `${SEQ_CH_LOG_PATH}` (`seq_trace.jsonl`) rows

Both sources are converted into OTLP spans and sent to `${SEQ_MAPLE_FORWARDER_ENDPOINT}` with `x-maple-ingest-key`.

## Configure

```bash
cd ~/code/seq
f env set --personal SEQ_MAPLE_FORWARDER_ENDPOINT=https://ingest.maple.dev/v1/traces
f env set --personal SEQ_MAPLE_FORWARDER_INGEST_KEY=maple_pk_xxx
f env set --personal SEQ_MEM_SINK_MODE=file
f ch-mode file
```

TLS note:
- `SEQ_MAPLE_FORWARDER_VERIFY_TLS=true` (default)
- optional custom CA bundle path: `SEQ_MAPLE_FORWARDER_CA_BUNDLE=/path/to/ca.pem`

## Run

```bash
f maple-forwarder-preflight
f maple-forwarder-on
f maple-forwarder-status
f maple-forwarder-logs
```

One-shot flush:

```bash
f maple-forwarder-once
```

## Launchd Integration

`f seq-harbor-run` includes this service under launchd label:

- `dev.nikiv.seq-capture.maple-forwarder`

This keeps forwarding always-on across reboots/crashes.
