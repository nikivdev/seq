# seq_client

Rust client for `seqd` Agent RPC v1 over Unix sockets.

## Install (path dependency)

```toml
[dependencies]
seq_client = { path = "/Users/nikiv/code/seq/api/rust/seq_client" }
```

## Example

```rust
use seq_client::{RpcRequest, SeqClient};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = SeqClient::connect_default()?;
    let ping = client.ping()?;
    println!("ping ok={} dur_us={}", ping.ok, ping.dur_us);

    let resp = client.call(
        RpcRequest::new("open_app")
            .with_request_id("req-1")
            .with_run_id("run-1")
            .with_tool_call_id("tool-1")
            .with_args_json(serde_json::json!({ "name": "Safari" })),
    )?;
    println!("open_app ok={}", resp.ok);
    Ok(())
}
```

See `docs/agent-rpc-v1.md` in this repo for RPC schema and operation list.

## Runnable example

Run the end-to-end example:

```bash
cd /Users/nikiv/code/seq/api/rust/seq_client
cargo run --example agent_loop
```

Environment overrides:
- `SEQ_SOCKET_PATH` (default `/tmp/seqd.sock`)
- `SEQ_RUN_ID` (default `agent-loop-example`)
- `SEQ_APP` (default `Safari`)
- `SEQ_SCREENSHOT_PATH` (default `/tmp/seq-agent-loop.png`)
