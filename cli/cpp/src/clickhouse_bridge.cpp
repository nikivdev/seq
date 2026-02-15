#include "clickhouse_bridge.h"
#include "clickhouse.h"

#include <optional>
#include <string>

struct seq_ch_writer {
    seq::ch::AsyncWriter writer;
    explicit seq_ch_writer(seq::ch::Config cfg) : writer(std::move(cfg)) {}
};

extern "C" {

seq_ch_writer_t* seq_ch_writer_create(const char* host,
                                       uint16_t port,
                                       const char* database) {
    try {
        seq::ch::Config cfg;
        if (host) cfg.host = host;
        cfg.port = port;
        if (database) cfg.database = database;
        return new seq_ch_writer(std::move(cfg));
    } catch (...) {
        return nullptr;
    }
}

void seq_ch_writer_destroy(seq_ch_writer_t* w) {
    delete w;
}

void seq_ch_push_mem_event(seq_ch_writer_t* w,
                           uint64_t ts_ms,
                           uint64_t dur_us,
                           uint8_t ok,
                           const char* session_id,
                           const char* event_id,
                           const char* content_hash,
                           const char* name,
                           const char* subject) {
    if (!w) return;
    seq::ch::MemEventRow row;
    row.ts_ms = ts_ms;
    row.dur_us = dur_us;
    row.ok = (ok != 0);
    if (session_id) row.session_id = session_id;
    if (event_id) row.event_id = event_id;
    if (content_hash) row.content_hash = content_hash;
    if (name) row.name = name;
    if (subject) row.subject = std::string(subject);
    w->writer.PushMemEvent(std::move(row));
}

void seq_ch_push_trace_event(seq_ch_writer_t* w,
                             int64_t ts_us,
                             const char* app,
                             uint32_t pid,
                             uint64_t tid,
                             const char* level,
                             const char* kind,
                             const char* name,
                             const char* message,
                             int64_t dur_us) {
    if (!w) return;
    seq::ch::TraceEventRow row;
    row.ts_us = ts_us;
    if (app) row.app = app;
    row.pid = pid;
    row.tid = tid;
    if (level) row.level = level;
    if (kind) row.kind = kind;
    if (name) row.name = name;
    if (message) row.message = message;
    row.dur_us = dur_us;
    w->writer.PushTraceEvent(std::move(row));
}

void seq_ch_flush(seq_ch_writer_t* w) {
    if (!w) return;
    w->writer.Flush();
}

uint64_t seq_ch_error_count(const seq_ch_writer_t* w) {
    if (!w) return 0;
    return w->writer.ErrorCount();
}

uint64_t seq_ch_inserted_count(const seq_ch_writer_t* w) {
    if (!w) return 0;
    return w->writer.InsertedCount();
}

void seq_ch_push_context(seq_ch_writer_t* w,
                          uint64_t ts_ms,
                          uint64_t dur_ms,
                          const char* app,
                          const char* bundle_id,
                          const char* window_title,
                          const char* url,
                          uint8_t afk) {
    if (!w) return;
    seq::ch::ContextRow row;
    row.ts_ms = ts_ms;
    row.dur_ms = dur_ms;
    if (app) row.app = app;
    if (bundle_id) row.bundle_id = bundle_id;
    if (window_title) row.window_title = window_title;
    if (url) row.url = url;
    row.afk = afk;
    w->writer.PushContext(std::move(row));
}

void seq_ch_push_superstep(seq_ch_writer_t* w,
                            uint64_t ts_ms,
                            const char* thread_id,
                            const char* graph_name,
                            uint32_t graph_version,
                            uint32_t step_index,
                            uint32_t frontier_count,
                            uint32_t writes,
                            uint64_t dur_us,
                            uint8_t status) {
    if (!w) return;
    seq::ch::SuperstepRow row;
    row.ts_ms = ts_ms;
    if (thread_id) row.thread_id = thread_id;
    if (graph_name) row.graph_name = graph_name;
    row.graph_version = graph_version;
    row.step_index = step_index;
    row.frontier_count = frontier_count;
    row.writes = writes;
    row.dur_us = dur_us;
    row.status = status;
    w->writer.PushSuperstep(std::move(row));
}

void seq_ch_push_model_invocation(seq_ch_writer_t* w,
                                   uint64_t ts_ms,
                                   const char* thread_id,
                                   const char* node_id,
                                   const char* graph_name,
                                   const char* provider,
                                   const char* model,
                                   uint32_t input_tokens,
                                   uint32_t output_tokens,
                                   uint64_t dur_us,
                                   uint64_t ttft_us,
                                   uint16_t tool_calls,
                                   uint8_t ok,
                                   const char* error_msg) {
    if (!w) return;
    seq::ch::ModelInvocationRow row;
    row.ts_ms = ts_ms;
    if (thread_id) row.thread_id = thread_id;
    if (node_id) row.node_id = node_id;
    if (graph_name) row.graph_name = graph_name;
    if (provider) row.provider = provider;
    if (model) row.model = model;
    row.input_tokens = input_tokens;
    row.output_tokens = output_tokens;
    row.dur_us = dur_us;
    row.ttft_us = ttft_us;
    row.tool_calls = tool_calls;
    row.ok = ok;
    if (error_msg) row.error_msg = error_msg;
    w->writer.PushModelInvocation(std::move(row));
}

void seq_ch_push_tool_call(seq_ch_writer_t* w,
                            uint64_t ts_ms,
                            const char* thread_id,
                            const char* node_id,
                            const char* tool_name,
                            const char* input_json,
                            const char* output_json,
                            uint64_t dur_us,
                            uint8_t ok) {
    if (!w) return;
    seq::ch::ToolCallRow row;
    row.ts_ms = ts_ms;
    if (thread_id) row.thread_id = thread_id;
    if (node_id) row.node_id = node_id;
    if (tool_name) row.tool_name = tool_name;
    if (input_json) row.input_json = input_json;
    if (output_json) row.output_json = output_json;
    row.dur_us = dur_us;
    row.ok = ok;
    w->writer.PushToolCall(std::move(row));
}

void seq_ch_push_agent_session(seq_ch_writer_t* w,
                               uint64_t ts_ms,
                               const char* session_id,
                               const char* agent,
                               const char* model,
                               const char* project_path,
                               const char* git_branch,
                               const char* git_commit,
                               uint64_t dur_ms,
                               uint32_t turns,
                               uint64_t total_input_tokens,
                               uint64_t total_output_tokens,
                               double total_cost_usd) {
    if (!w) return;
    seq::ch::AgentSessionRow row;
    row.ts_ms = ts_ms;
    if (session_id) row.session_id = session_id;
    if (agent) row.agent = agent;
    if (model) row.model = model;
    if (project_path) row.project_path = project_path;
    if (git_branch) row.git_branch = git_branch;
    if (git_commit) row.git_commit = git_commit;
    row.dur_ms = dur_ms;
    row.turns = turns;
    row.total_input_tokens = total_input_tokens;
    row.total_output_tokens = total_output_tokens;
    row.total_cost_usd = total_cost_usd;
    w->writer.PushAgentSession(std::move(row));
}

void seq_ch_push_agent_turn(seq_ch_writer_t* w,
                            uint64_t ts_ms,
                            const char* session_id,
                            uint32_t turn_index,
                            const char* agent,
                            const char* model,
                            uint32_t input_tokens,
                            uint32_t output_tokens,
                            uint32_t cached_tokens,
                            uint32_t reasoning_tokens,
                            uint32_t dur_ms,
                            double cost_usd,
                            const char* stop_reason,
                            uint8_t is_error,
                            uint32_t context_window,
                            float context_used_pct) {
    if (!w) return;
    seq::ch::AgentTurnRow row;
    row.ts_ms = ts_ms;
    if (session_id) row.session_id = session_id;
    row.turn_index = turn_index;
    if (agent) row.agent = agent;
    if (model) row.model = model;
    row.input_tokens = input_tokens;
    row.output_tokens = output_tokens;
    row.cached_tokens = cached_tokens;
    row.reasoning_tokens = reasoning_tokens;
    row.dur_ms = dur_ms;
    row.cost_usd = cost_usd;
    if (stop_reason) row.stop_reason = stop_reason;
    row.is_error = is_error;
    row.context_window = context_window;
    row.context_used_pct = context_used_pct;
    w->writer.PushAgentTurn(std::move(row));
}

void seq_ch_push_agent_tool_call(seq_ch_writer_t* w,
                                 uint64_t ts_ms,
                                 const char* session_id,
                                 uint32_t turn_index,
                                 const char* agent,
                                 const char* tool_name,
                                 const char* input_summary,
                                 uint32_t dur_ms,
                                 uint8_t ok,
                                 uint32_t output_lines,
                                 uint32_t output_bytes) {
    if (!w) return;
    seq::ch::AgentToolCallRow row;
    row.ts_ms = ts_ms;
    if (session_id) row.session_id = session_id;
    row.turn_index = turn_index;
    if (agent) row.agent = agent;
    if (tool_name) row.tool_name = tool_name;
    if (input_summary) row.input_summary = input_summary;
    row.dur_ms = dur_ms;
    row.ok = ok;
    row.output_lines = output_lines;
    row.output_bytes = output_bytes;
    w->writer.PushAgentToolCall(std::move(row));
}

} // extern "C"
