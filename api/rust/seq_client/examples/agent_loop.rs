use seq_client::{RpcRequest, SeqClient};
use serde_json::json;
use std::env;
use std::time::Duration;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let socket_path = env::var("SEQ_SOCKET_PATH").unwrap_or_else(|_| "/tmp/seqd.sock".to_string());
    let run_id = env::var("SEQ_RUN_ID").unwrap_or_else(|_| "agent-loop-example".to_string());
    let app = env::var("SEQ_APP").unwrap_or_else(|_| "Safari".to_string());
    let screenshot_path =
        env::var("SEQ_SCREENSHOT_PATH").unwrap_or_else(|_| "/tmp/seq-agent-loop.png".to_string());

    let client = SeqClient::connect_with_timeout(&socket_path, Duration::from_secs(5))?;

    let ping = client
        .call(
            RpcRequest::new("ping")
                .with_request_id("boot-ping")
                .with_run_id(run_id.clone())
                .with_tool_call_id("tool-ping"),
        )?;
    println!("ping: ok={} dur_us={}", ping.ok, ping.dur_us);
    if !ping.ok {
        return Err(format!("ping failed: {:?}", ping.error).into());
    }

    let open = client.call(
        RpcRequest::new("open_app")
            .with_request_id("open-app")
            .with_run_id(run_id.clone())
            .with_tool_call_id("tool-open-app")
            .with_args_json(json!({ "name": app })),
    )?;
    println!("open_app: ok={} err={:?}", open.ok, open.error);

    let shot = client.call(
        RpcRequest::new("screenshot")
            .with_request_id("screenshot")
            .with_run_id(run_id.clone())
            .with_tool_call_id("tool-screenshot")
            .with_args_json(json!({ "path": screenshot_path })),
    )?;
    println!("screenshot: ok={} result={:?}", shot.ok, shot.result);

    let app_state = client.call(
        RpcRequest::new("app_state")
            .with_request_id("app-state")
            .with_run_id(run_id)
            .with_tool_call_id("tool-app-state"),
    )?;
    println!("app_state: ok={} result={:?}", app_state.ok, app_state.result);

    Ok(())
}

