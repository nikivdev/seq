use seq_everruns_bridge::client_side_tool_definitions;

fn main() {
    let defs = client_side_tool_definitions();
    println!("{}", serde_json::to_string_pretty(&defs).unwrap());
}
