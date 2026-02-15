#include "clickhouse.h"

#include <clickhouse/client.h>

namespace seq::ch {

// ─── Client (pimpl) ─────────────────────────────────────────────────────────

struct Client::Impl {
    Config config;
    std::unique_ptr<clickhouse::Client> client;

    explicit Impl(Config cfg) : config(std::move(cfg)) {
        Reconnect();
    }

    void Reconnect() {
        clickhouse::ClientOptions opts;
        opts.SetHost(config.host);
        opts.SetPort(config.port);
        opts.SetDefaultDatabase(config.database);
        opts.SetCompressionMethod(clickhouse::CompressionMethod::LZ4);
        client = std::make_unique<clickhouse::Client>(opts);
    }
};

Client::Client(Config config)
    : impl_(std::make_unique<Impl>(std::move(config))) {}

Client::~Client() = default;

bool Client::IsAlive() const noexcept {
    try {
        impl_->client->Ping();
        return true;
    } catch (...) {
        return false;
    }
}

size_t Client::InsertMemEvents(std::span<const MemEventRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_dur_us = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_ok = std::make_shared<clickhouse::ColumnUInt8>();
    auto col_session_id = std::make_shared<clickhouse::ColumnString>();
    auto col_event_id = std::make_shared<clickhouse::ColumnString>();
    auto col_content_hash = std::make_shared<clickhouse::ColumnString>();
    auto col_name = std::make_shared<clickhouse::ColumnString>();
    auto col_subject_data = std::make_shared<clickhouse::ColumnString>();
    auto col_subject_nulls = std::make_shared<clickhouse::ColumnUInt8>();

    for (const auto& row : rows) {
        col_ts_ms->Append(row.ts_ms);
        col_dur_us->Append(row.dur_us);
        col_ok->Append(row.ok ? uint8_t{1} : uint8_t{0});
        col_session_id->Append(row.session_id);
        col_event_id->Append(row.event_id);
        col_content_hash->Append(row.content_hash);
        col_name->Append(row.name);
        if (row.subject.has_value()) {
            col_subject_data->Append(*row.subject);
            col_subject_nulls->Append(uint8_t{0});
        } else {
            col_subject_data->Append(std::string_view{});
            col_subject_nulls->Append(uint8_t{1});
        }
    }

    auto col_subject = std::make_shared<clickhouse::ColumnNullable>(
        col_subject_data, col_subject_nulls);

    clickhouse::Block block;
    block.AppendColumn("ts_ms", col_ts_ms);
    block.AppendColumn("dur_us", col_dur_us);
    block.AppendColumn("ok", col_ok);
    block.AppendColumn("session_id", col_session_id);
    block.AppendColumn("event_id", col_event_id);
    block.AppendColumn("content_hash", col_content_hash);
    block.AppendColumn("name", col_name);
    block.AppendColumn("subject", col_subject);

    impl_->client->Insert("mem_events", block);
    return rows.size();
}

size_t Client::InsertTraceEvents(std::span<const TraceEventRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_us = std::make_shared<clickhouse::ColumnInt64>();
    auto col_app = std::make_shared<clickhouse::ColumnString>();
    auto col_pid = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_tid = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_level = std::make_shared<clickhouse::ColumnString>();
    auto col_kind = std::make_shared<clickhouse::ColumnString>();
    auto col_name = std::make_shared<clickhouse::ColumnString>();
    auto col_message = std::make_shared<clickhouse::ColumnString>();
    auto col_dur_us = std::make_shared<clickhouse::ColumnInt64>();

    for (const auto& row : rows) {
        col_ts_us->Append(row.ts_us);
        col_app->Append(row.app);
        col_pid->Append(row.pid);
        col_tid->Append(row.tid);
        col_level->Append(row.level);
        col_kind->Append(row.kind);
        col_name->Append(row.name);
        col_message->Append(row.message);
        col_dur_us->Append(row.dur_us);
    }

    clickhouse::Block block;
    block.AppendColumn("ts_us", col_ts_us);
    block.AppendColumn("app", col_app);
    block.AppendColumn("pid", col_pid);
    block.AppendColumn("tid", col_tid);
    block.AppendColumn("level", col_level);
    block.AppendColumn("kind", col_kind);
    block.AppendColumn("name", col_name);
    block.AppendColumn("message", col_message);
    block.AppendColumn("dur_us", col_dur_us);

    impl_->client->Insert("trace_events", block);
    return rows.size();
}

size_t Client::InsertContextRows(std::span<const ContextRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_dur_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_app = std::make_shared<clickhouse::ColumnString>();
    auto col_bundle_id = std::make_shared<clickhouse::ColumnString>();
    auto col_window_title = std::make_shared<clickhouse::ColumnString>();
    auto col_url = std::make_shared<clickhouse::ColumnString>();
    auto col_afk = std::make_shared<clickhouse::ColumnUInt8>();

    for (const auto& row : rows) {
        col_ts_ms->Append(row.ts_ms);
        col_dur_ms->Append(row.dur_ms);
        col_app->Append(row.app);
        col_bundle_id->Append(row.bundle_id);
        col_window_title->Append(row.window_title);
        col_url->Append(row.url);
        col_afk->Append(row.afk);
    }

    clickhouse::Block block;
    block.AppendColumn("ts_ms", col_ts_ms);
    block.AppendColumn("dur_ms", col_dur_ms);
    block.AppendColumn("app", col_app);
    block.AppendColumn("bundle_id", col_bundle_id);
    block.AppendColumn("window_title", col_window_title);
    block.AppendColumn("url", col_url);
    block.AppendColumn("afk", col_afk);

    impl_->client->Insert("seq.context", block);
    return rows.size();
}

size_t Client::InsertSupersteps(std::span<const SuperstepRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_thread_id = std::make_shared<clickhouse::ColumnString>();
    auto col_graph_name = std::make_shared<clickhouse::ColumnString>();
    auto col_graph_version = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_step_index = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_frontier_count = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_writes = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_dur_us = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_status = std::make_shared<clickhouse::ColumnInt8>();

    for (const auto& row : rows) {
        col_ts_ms->Append(row.ts_ms);
        col_thread_id->Append(row.thread_id);
        col_graph_name->Append(row.graph_name);
        col_graph_version->Append(row.graph_version);
        col_step_index->Append(row.step_index);
        col_frontier_count->Append(row.frontier_count);
        col_writes->Append(row.writes);
        col_dur_us->Append(row.dur_us);
        col_status->Append(static_cast<int8_t>(row.status));
    }

    clickhouse::Block block;
    block.AppendColumn("ts_ms", col_ts_ms);
    block.AppendColumn("thread_id", col_thread_id);
    block.AppendColumn("graph_name", col_graph_name);
    block.AppendColumn("graph_version", col_graph_version);
    block.AppendColumn("step_index", col_step_index);
    block.AppendColumn("frontier_count", col_frontier_count);
    block.AppendColumn("writes", col_writes);
    block.AppendColumn("dur_us", col_dur_us);
    block.AppendColumn("status", col_status);

    impl_->client->Insert("hive.supersteps", block);
    return rows.size();
}

size_t Client::InsertModelInvocations(std::span<const ModelInvocationRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_thread_id = std::make_shared<clickhouse::ColumnString>();
    auto col_node_id = std::make_shared<clickhouse::ColumnString>();
    auto col_graph_name = std::make_shared<clickhouse::ColumnString>();
    auto col_provider = std::make_shared<clickhouse::ColumnString>();
    auto col_model = std::make_shared<clickhouse::ColumnString>();
    auto col_input_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_output_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_dur_us = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_ttft_us = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_tool_calls = std::make_shared<clickhouse::ColumnUInt16>();
    auto col_ok = std::make_shared<clickhouse::ColumnUInt8>();
    auto col_error_msg = std::make_shared<clickhouse::ColumnString>();

    for (const auto& row : rows) {
        col_ts_ms->Append(row.ts_ms);
        col_thread_id->Append(row.thread_id);
        col_node_id->Append(row.node_id);
        col_graph_name->Append(row.graph_name);
        col_provider->Append(row.provider);
        col_model->Append(row.model);
        col_input_tokens->Append(row.input_tokens);
        col_output_tokens->Append(row.output_tokens);
        col_dur_us->Append(row.dur_us);
        col_ttft_us->Append(row.ttft_us);
        col_tool_calls->Append(row.tool_calls);
        col_ok->Append(row.ok);
        col_error_msg->Append(row.error_msg);
    }

    clickhouse::Block block;
    block.AppendColumn("ts_ms", col_ts_ms);
    block.AppendColumn("thread_id", col_thread_id);
    block.AppendColumn("node_id", col_node_id);
    block.AppendColumn("graph_name", col_graph_name);
    block.AppendColumn("provider", col_provider);
    block.AppendColumn("model", col_model);
    block.AppendColumn("input_tokens", col_input_tokens);
    block.AppendColumn("output_tokens", col_output_tokens);
    block.AppendColumn("dur_us", col_dur_us);
    block.AppendColumn("ttft_us", col_ttft_us);
    block.AppendColumn("tool_calls", col_tool_calls);
    block.AppendColumn("ok", col_ok);
    block.AppendColumn("error_msg", col_error_msg);

    impl_->client->Insert("hive.model_invocations", block);
    return rows.size();
}

size_t Client::InsertToolCalls(std::span<const ToolCallRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_thread_id = std::make_shared<clickhouse::ColumnString>();
    auto col_node_id = std::make_shared<clickhouse::ColumnString>();
    auto col_tool_name = std::make_shared<clickhouse::ColumnString>();
    auto col_input_json = std::make_shared<clickhouse::ColumnString>();
    auto col_output_json = std::make_shared<clickhouse::ColumnString>();
    auto col_dur_us = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_ok = std::make_shared<clickhouse::ColumnUInt8>();

    for (const auto& row : rows) {
        col_ts_ms->Append(row.ts_ms);
        col_thread_id->Append(row.thread_id);
        col_node_id->Append(row.node_id);
        col_tool_name->Append(row.tool_name);
        col_input_json->Append(row.input_json);
        col_output_json->Append(row.output_json);
        col_dur_us->Append(row.dur_us);
        col_ok->Append(row.ok);
    }

    clickhouse::Block block;
    block.AppendColumn("ts_ms", col_ts_ms);
    block.AppendColumn("thread_id", col_thread_id);
    block.AppendColumn("node_id", col_node_id);
    block.AppendColumn("tool_name", col_tool_name);
    block.AppendColumn("input_json", col_input_json);
    block.AppendColumn("output_json", col_output_json);
    block.AppendColumn("dur_us", col_dur_us);
    block.AppendColumn("ok", col_ok);

    impl_->client->Insert("hive.tool_calls", block);
    return rows.size();
}

size_t Client::InsertAgentSessions(std::span<const AgentSessionRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_session_id = std::make_shared<clickhouse::ColumnString>();
    auto col_agent = std::make_shared<clickhouse::ColumnString>();
    auto col_model = std::make_shared<clickhouse::ColumnString>();
    auto col_project_path = std::make_shared<clickhouse::ColumnString>();
    auto col_git_branch = std::make_shared<clickhouse::ColumnString>();
    auto col_git_commit = std::make_shared<clickhouse::ColumnString>();
    auto col_dur_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_turns = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_total_input_tokens = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_total_output_tokens = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_total_cost_usd = std::make_shared<clickhouse::ColumnFloat64>();

    for (const auto& row : rows) {
        col_ts_ms->Append(row.ts_ms);
        col_session_id->Append(row.session_id);
        col_agent->Append(row.agent);
        col_model->Append(row.model);
        col_project_path->Append(row.project_path);
        col_git_branch->Append(row.git_branch);
        col_git_commit->Append(row.git_commit);
        col_dur_ms->Append(row.dur_ms);
        col_turns->Append(row.turns);
        col_total_input_tokens->Append(row.total_input_tokens);
        col_total_output_tokens->Append(row.total_output_tokens);
        col_total_cost_usd->Append(row.total_cost_usd);
    }

    clickhouse::Block block;
    block.AppendColumn("ts_ms", col_ts_ms);
    block.AppendColumn("session_id", col_session_id);
    block.AppendColumn("agent", col_agent);
    block.AppendColumn("model", col_model);
    block.AppendColumn("project_path", col_project_path);
    block.AppendColumn("git_branch", col_git_branch);
    block.AppendColumn("git_commit", col_git_commit);
    block.AppendColumn("dur_ms", col_dur_ms);
    block.AppendColumn("turns", col_turns);
    block.AppendColumn("total_input_tokens", col_total_input_tokens);
    block.AppendColumn("total_output_tokens", col_total_output_tokens);
    block.AppendColumn("total_cost_usd", col_total_cost_usd);

    impl_->client->Insert("agent.sessions", block);
    return rows.size();
}

size_t Client::InsertAgentTurns(std::span<const AgentTurnRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_session_id = std::make_shared<clickhouse::ColumnString>();
    auto col_turn_index = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_agent = std::make_shared<clickhouse::ColumnString>();
    auto col_model = std::make_shared<clickhouse::ColumnString>();
    auto col_input_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_output_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_cached_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_reasoning_tokens = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_dur_ms = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_cost_usd = std::make_shared<clickhouse::ColumnFloat64>();
    auto col_stop_reason = std::make_shared<clickhouse::ColumnString>();
    auto col_is_error = std::make_shared<clickhouse::ColumnUInt8>();
    auto col_context_window = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_context_used_pct = std::make_shared<clickhouse::ColumnFloat32>();

    for (const auto& row : rows) {
        col_ts_ms->Append(row.ts_ms);
        col_session_id->Append(row.session_id);
        col_turn_index->Append(row.turn_index);
        col_agent->Append(row.agent);
        col_model->Append(row.model);
        col_input_tokens->Append(row.input_tokens);
        col_output_tokens->Append(row.output_tokens);
        col_cached_tokens->Append(row.cached_tokens);
        col_reasoning_tokens->Append(row.reasoning_tokens);
        col_dur_ms->Append(row.dur_ms);
        col_cost_usd->Append(row.cost_usd);
        col_stop_reason->Append(row.stop_reason);
        col_is_error->Append(row.is_error);
        col_context_window->Append(row.context_window);
        col_context_used_pct->Append(row.context_used_pct);
    }

    clickhouse::Block block;
    block.AppendColumn("ts_ms", col_ts_ms);
    block.AppendColumn("session_id", col_session_id);
    block.AppendColumn("turn_index", col_turn_index);
    block.AppendColumn("agent", col_agent);
    block.AppendColumn("model", col_model);
    block.AppendColumn("input_tokens", col_input_tokens);
    block.AppendColumn("output_tokens", col_output_tokens);
    block.AppendColumn("cached_tokens", col_cached_tokens);
    block.AppendColumn("reasoning_tokens", col_reasoning_tokens);
    block.AppendColumn("dur_ms", col_dur_ms);
    block.AppendColumn("cost_usd", col_cost_usd);
    block.AppendColumn("stop_reason", col_stop_reason);
    block.AppendColumn("is_error", col_is_error);
    block.AppendColumn("context_window", col_context_window);
    block.AppendColumn("context_used_pct", col_context_used_pct);

    impl_->client->Insert("agent.turns", block);
    return rows.size();
}

size_t Client::InsertAgentToolCalls(std::span<const AgentToolCallRow> rows) {
    if (rows.empty()) return 0;

    auto col_ts_ms = std::make_shared<clickhouse::ColumnUInt64>();
    auto col_session_id = std::make_shared<clickhouse::ColumnString>();
    auto col_turn_index = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_agent = std::make_shared<clickhouse::ColumnString>();
    auto col_tool_name = std::make_shared<clickhouse::ColumnString>();
    auto col_input_summary = std::make_shared<clickhouse::ColumnString>();
    auto col_dur_ms = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_ok = std::make_shared<clickhouse::ColumnUInt8>();
    auto col_output_lines = std::make_shared<clickhouse::ColumnUInt32>();
    auto col_output_bytes = std::make_shared<clickhouse::ColumnUInt32>();

    for (const auto& row : rows) {
        col_ts_ms->Append(row.ts_ms);
        col_session_id->Append(row.session_id);
        col_turn_index->Append(row.turn_index);
        col_agent->Append(row.agent);
        col_tool_name->Append(row.tool_name);
        col_input_summary->Append(row.input_summary);
        col_dur_ms->Append(row.dur_ms);
        col_ok->Append(row.ok);
        col_output_lines->Append(row.output_lines);
        col_output_bytes->Append(row.output_bytes);
    }

    clickhouse::Block block;
    block.AppendColumn("ts_ms", col_ts_ms);
    block.AppendColumn("session_id", col_session_id);
    block.AppendColumn("turn_index", col_turn_index);
    block.AppendColumn("agent", col_agent);
    block.AppendColumn("tool_name", col_tool_name);
    block.AppendColumn("input_summary", col_input_summary);
    block.AppendColumn("dur_ms", col_dur_ms);
    block.AppendColumn("ok", col_ok);
    block.AppendColumn("output_lines", col_output_lines);
    block.AppendColumn("output_bytes", col_output_bytes);

    impl_->client->Insert("agent.tool_calls", block);
    return rows.size();
}

void Client::Execute(std::string_view sql) {
    impl_->client->Execute(std::string(sql));
}

// ─── AsyncWriter ────────────────────────────────────────────────────────────

AsyncWriter::AsyncWriter(Config config)
    : config_(std::move(config)) {
    flush_thread_ = std::thread([this] { FlushThread(); });
}

AsyncWriter::~AsyncWriter() {
    stop_.store(true, std::memory_order_release);
    cv_.notify_one();
    if (flush_thread_.joinable()) {
        flush_thread_.join();
    }
}

void AsyncWriter::PushMemEvent(MemEventRow row) noexcept {
    try {
        std::lock_guard lock(mu_);
        mem_pending_.push_back(std::move(row));
        if (mem_pending_.size() >= config_.batch_size) {
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::PushTraceEvent(TraceEventRow row) noexcept {
    try {
        std::lock_guard lock(mu_);
        trace_pending_.push_back(std::move(row));
        if (trace_pending_.size() >= config_.batch_size) {
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::PushContext(ContextRow row) noexcept {
    try {
        std::lock_guard lock(mu_);
        ctx_pending_.push_back(std::move(row));
        if (ctx_pending_.size() >= config_.batch_size) {
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::PushSuperstep(SuperstepRow row) noexcept {
    try {
        std::lock_guard lock(mu_);
        superstep_pending_.push_back(std::move(row));
        if (superstep_pending_.size() >= config_.batch_size) {
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::PushModelInvocation(ModelInvocationRow row) noexcept {
    try {
        std::lock_guard lock(mu_);
        model_pending_.push_back(std::move(row));
        if (model_pending_.size() >= config_.batch_size) {
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::PushToolCall(ToolCallRow row) noexcept {
    try {
        std::lock_guard lock(mu_);
        tool_pending_.push_back(std::move(row));
        if (tool_pending_.size() >= config_.batch_size) {
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::PushAgentSession(AgentSessionRow row) noexcept {
    try {
        std::lock_guard lock(mu_);
        agent_session_pending_.push_back(std::move(row));
        if (agent_session_pending_.size() >= config_.batch_size) {
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::PushAgentTurn(AgentTurnRow row) noexcept {
    try {
        std::lock_guard lock(mu_);
        agent_turn_pending_.push_back(std::move(row));
        if (agent_turn_pending_.size() >= config_.batch_size) {
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::PushAgentToolCall(AgentToolCallRow row) noexcept {
    try {
        std::lock_guard lock(mu_);
        agent_tool_pending_.push_back(std::move(row));
        if (agent_tool_pending_.size() >= config_.batch_size) {
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::Flush() {
    cv_.notify_one();
}

size_t AsyncWriter::PendingCount() const noexcept {
    std::lock_guard lock(mu_);
    return mem_pending_.size() + trace_pending_.size() +
           ctx_pending_.size() + superstep_pending_.size() +
           model_pending_.size() + tool_pending_.size() +
           agent_session_pending_.size() + agent_turn_pending_.size() +
           agent_tool_pending_.size();
}

uint64_t AsyncWriter::ErrorCount() const noexcept {
    return error_count_.load(std::memory_order_relaxed);
}

uint64_t AsyncWriter::InsertedCount() const noexcept {
    return inserted_count_.load(std::memory_order_relaxed);
}

template <typename T>
static void drain_queue(std::vector<T>& pending, std::vector<T>& batch, uint32_t batch_size) {
    if (pending.size() <= batch_size) {
        batch.swap(pending);
    } else {
        batch.assign(
            std::make_move_iterator(pending.begin()),
            std::make_move_iterator(pending.begin() + static_cast<ptrdiff_t>(batch_size)));
        pending.erase(pending.begin(),
                      pending.begin() + static_cast<ptrdiff_t>(batch_size));
    }
}

void AsyncWriter::DrainAndInsert(Client& client) {
    std::vector<MemEventRow> mem_batch;
    std::vector<TraceEventRow> trace_batch;
    std::vector<ContextRow> ctx_batch;
    std::vector<SuperstepRow> superstep_batch;
    std::vector<ModelInvocationRow> model_batch;
    std::vector<ToolCallRow> tool_batch;
    std::vector<AgentSessionRow> agent_session_batch;
    std::vector<AgentTurnRow> agent_turn_batch;
    std::vector<AgentToolCallRow> agent_tool_batch;

    {
        std::lock_guard lock(mu_);
        drain_queue(mem_pending_, mem_batch, config_.batch_size);
        drain_queue(trace_pending_, trace_batch, config_.batch_size);
        drain_queue(ctx_pending_, ctx_batch, config_.batch_size);
        drain_queue(superstep_pending_, superstep_batch, config_.batch_size);
        drain_queue(model_pending_, model_batch, config_.batch_size);
        drain_queue(tool_pending_, tool_batch, config_.batch_size);
        drain_queue(agent_session_pending_, agent_session_batch, config_.batch_size);
        drain_queue(agent_turn_pending_, agent_turn_batch, config_.batch_size);
        drain_queue(agent_tool_pending_, agent_tool_batch, config_.batch_size);
    }

    if (!mem_batch.empty()) {
        auto n = client.InsertMemEvents(mem_batch);
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!trace_batch.empty()) {
        auto n = client.InsertTraceEvents(trace_batch);
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!ctx_batch.empty()) {
        auto n = client.InsertContextRows(ctx_batch);
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!superstep_batch.empty()) {
        auto n = client.InsertSupersteps(superstep_batch);
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!model_batch.empty()) {
        auto n = client.InsertModelInvocations(model_batch);
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!tool_batch.empty()) {
        auto n = client.InsertToolCalls(tool_batch);
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!agent_session_batch.empty()) {
        auto n = client.InsertAgentSessions(agent_session_batch);
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!agent_turn_batch.empty()) {
        auto n = client.InsertAgentTurns(agent_turn_batch);
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!agent_tool_batch.empty()) {
        auto n = client.InsertAgentToolCalls(agent_tool_batch);
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
}

void AsyncWriter::FlushThread() {
    std::unique_ptr<Client> client;

    while (!stop_.load(std::memory_order_acquire)) {
        {
            std::unique_lock lock(mu_);
            cv_.wait_for(lock, std::chrono::milliseconds(config_.flush_interval_ms), [this] {
                return stop_.load(std::memory_order_relaxed) ||
                       mem_pending_.size() >= config_.batch_size ||
                       trace_pending_.size() >= config_.batch_size;
            });
        }

        // Lazy connect
        if (!client) {
            try {
                client = std::make_unique<Client>(config_);
            } catch (...) {
                error_count_.fetch_add(1, std::memory_order_relaxed);
                continue;
            }
        }

        try {
            DrainAndInsert(*client);
        } catch (...) {
            error_count_.fetch_add(1, std::memory_order_relaxed);
            // Reset client to force reconnect on next iteration
            client.reset();
        }
    }

    // Final drain on shutdown
    if (client) {
        try {
            DrainAndInsert(*client);
        } catch (...) {
            // Best effort
        }
    }
}

} // namespace seq::ch
