#include "clickhouse.h"

#include <clickhouse/client.h>
#include <algorithm>
#include <chrono>
#include <iterator>

namespace seq::ch {

namespace {
void store_max(std::atomic<uint64_t>& target, uint64_t value) {
    uint64_t current = target.load(std::memory_order_relaxed);
    while (value > current && !target.compare_exchange_weak(current, value, std::memory_order_relaxed)) {
    }
}

template <typename T>
size_t pending_size(const std::vector<T>& pending, size_t head) {
    return pending.size() - head;
}
} // namespace

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
    const size_t reserve_count = static_cast<size_t>(config_.batch_size);
    mem_pending_.reserve(reserve_count);
    trace_pending_.reserve(reserve_count);
    ctx_pending_.reserve(reserve_count);
    superstep_pending_.reserve(reserve_count);
    model_pending_.reserve(reserve_count);
    tool_pending_.reserve(reserve_count);
    agent_session_pending_.reserve(reserve_count);
    agent_turn_pending_.reserve(reserve_count);
    agent_tool_pending_.reserve(reserve_count);
    mem_batch_.reserve(reserve_count);
    trace_batch_.reserve(reserve_count);
    ctx_batch_.reserve(reserve_count);
    superstep_batch_.reserve(reserve_count);
    model_batch_.reserve(reserve_count);
    tool_batch_.reserve(reserve_count);
    agent_session_batch_.reserve(reserve_count);
    agent_turn_batch_.reserve(reserve_count);
    agent_tool_batch_.reserve(reserve_count);
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
        ++pending_rows_;
        push_count_.fetch_add(1, std::memory_order_relaxed);
        UpdateMaxPendingNoLock();
        if (pending_size(mem_pending_, mem_head_) == config_.batch_size) {
            batch_ready_ = true;
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
        ++pending_rows_;
        push_count_.fetch_add(1, std::memory_order_relaxed);
        UpdateMaxPendingNoLock();
        if (pending_size(trace_pending_, trace_head_) == config_.batch_size) {
            batch_ready_ = true;
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
        ++pending_rows_;
        push_count_.fetch_add(1, std::memory_order_relaxed);
        UpdateMaxPendingNoLock();
        if (pending_size(ctx_pending_, ctx_head_) == config_.batch_size) {
            batch_ready_ = true;
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
        ++pending_rows_;
        push_count_.fetch_add(1, std::memory_order_relaxed);
        UpdateMaxPendingNoLock();
        if (pending_size(superstep_pending_, superstep_head_) == config_.batch_size) {
            batch_ready_ = true;
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
        ++pending_rows_;
        push_count_.fetch_add(1, std::memory_order_relaxed);
        UpdateMaxPendingNoLock();
        if (pending_size(model_pending_, model_head_) == config_.batch_size) {
            batch_ready_ = true;
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
        ++pending_rows_;
        push_count_.fetch_add(1, std::memory_order_relaxed);
        UpdateMaxPendingNoLock();
        if (pending_size(tool_pending_, tool_head_) == config_.batch_size) {
            batch_ready_ = true;
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
        ++pending_rows_;
        push_count_.fetch_add(1, std::memory_order_relaxed);
        UpdateMaxPendingNoLock();
        if (pending_size(agent_session_pending_, agent_session_head_) == config_.batch_size) {
            batch_ready_ = true;
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
        ++pending_rows_;
        push_count_.fetch_add(1, std::memory_order_relaxed);
        UpdateMaxPendingNoLock();
        if (pending_size(agent_turn_pending_, agent_turn_head_) == config_.batch_size) {
            batch_ready_ = true;
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
        ++pending_rows_;
        push_count_.fetch_add(1, std::memory_order_relaxed);
        UpdateMaxPendingNoLock();
        if (pending_size(agent_tool_pending_, agent_tool_head_) == config_.batch_size) {
            batch_ready_ = true;
            cv_.notify_one();
        }
    } catch (...) {
        error_count_.fetch_add(1, std::memory_order_relaxed);
    }
}

void AsyncWriter::Flush() {
    {
        std::lock_guard lock(mu_);
        flush_requested_ = true;
    }
    cv_.notify_one();
}

size_t AsyncWriter::PendingCount() const noexcept {
    std::lock_guard lock(mu_);
    return PendingCountNoLock();
}

uint64_t AsyncWriter::ErrorCount() const noexcept {
    return error_count_.load(std::memory_order_relaxed);
}

uint64_t AsyncWriter::InsertedCount() const noexcept {
    return inserted_count_.load(std::memory_order_relaxed);
}

AsyncWriterPerfSnapshot AsyncWriter::PerfSnapshot() const noexcept {
    AsyncWriterPerfSnapshot out;
    out.push_calls = push_count_.load(std::memory_order_relaxed);
    out.wake_count = wake_count_.load(std::memory_order_relaxed);
    out.flush_count = flush_count_.load(std::memory_order_relaxed);
    out.total_flush_us = total_flush_us_.load(std::memory_order_relaxed);
    out.max_flush_us = max_flush_us_.load(std::memory_order_relaxed);
    out.last_flush_us = last_flush_us_.load(std::memory_order_relaxed);
    out.last_flush_rows = last_flush_rows_.load(std::memory_order_relaxed);
    out.last_pending_rows = last_pending_rows_.load(std::memory_order_relaxed);
    out.max_pending_rows = max_pending_rows_.load(std::memory_order_relaxed);
    out.error_count = error_count_.load(std::memory_order_relaxed);
    out.inserted_count = inserted_count_.load(std::memory_order_relaxed);
    return out;
}

size_t AsyncWriter::PendingCountNoLock() const noexcept {
    return pending_rows_;
}

void AsyncWriter::UpdateMaxPendingNoLock() noexcept {
    auto pending = static_cast<uint64_t>(pending_rows_);
    last_pending_rows_.store(pending, std::memory_order_relaxed);
    store_max(max_pending_rows_, pending);
}

void AsyncWriter::RecomputeBatchReadyNoLock() noexcept {
    batch_ready_ =
        pending_size(mem_pending_, mem_head_) >= config_.batch_size ||
        pending_size(trace_pending_, trace_head_) >= config_.batch_size ||
        pending_size(ctx_pending_, ctx_head_) >= config_.batch_size ||
        pending_size(superstep_pending_, superstep_head_) >= config_.batch_size ||
        pending_size(model_pending_, model_head_) >= config_.batch_size ||
        pending_size(tool_pending_, tool_head_) >= config_.batch_size ||
        pending_size(agent_session_pending_, agent_session_head_) >= config_.batch_size ||
        pending_size(agent_turn_pending_, agent_turn_head_) >= config_.batch_size ||
        pending_size(agent_tool_pending_, agent_tool_head_) >= config_.batch_size;
}

template <typename T>
static size_t drain_queue(std::vector<T>& pending, size_t& head, std::vector<T>& batch, uint32_t batch_size) {
    const size_t available = pending_size(pending, head);
    if (!available) {
        return 0;
    }
    const size_t take = std::min<size_t>(available, static_cast<size_t>(batch_size));
    const auto begin = pending.begin() + static_cast<ptrdiff_t>(head);
    const auto end = begin + static_cast<ptrdiff_t>(take);
    batch.insert(batch.end(), std::make_move_iterator(begin), std::make_move_iterator(end));
    head += take;

    if (head == pending.size()) {
        pending.clear();
        head = 0;
        return take;
    }

    // Avoid O(n) front erase every drain. Compact only when head grows large.
    if (head >= pending.size() / 2) {
        auto new_end = std::move(pending.begin() + static_cast<ptrdiff_t>(head), pending.end(), pending.begin());
        pending.erase(new_end, pending.end());
        head = 0;
    }
    return take;
}

size_t AsyncWriter::DrainAndInsert(Client& client) {
    auto& mem_batch = mem_batch_;
    auto& trace_batch = trace_batch_;
    auto& ctx_batch = ctx_batch_;
    auto& superstep_batch = superstep_batch_;
    auto& model_batch = model_batch_;
    auto& tool_batch = tool_batch_;
    auto& agent_session_batch = agent_session_batch_;
    auto& agent_turn_batch = agent_turn_batch_;
    auto& agent_tool_batch = agent_tool_batch_;
    mem_batch.clear();
    trace_batch.clear();
    ctx_batch.clear();
    superstep_batch.clear();
    model_batch.clear();
    tool_batch.clear();
    agent_session_batch.clear();
    agent_turn_batch.clear();
    agent_tool_batch.clear();
    size_t inserted = 0;

    {
        std::lock_guard lock(mu_);
        size_t drained = 0;
        drained += drain_queue(mem_pending_, mem_head_, mem_batch, config_.batch_size);
        drained += drain_queue(trace_pending_, trace_head_, trace_batch, config_.batch_size);
        drained += drain_queue(ctx_pending_, ctx_head_, ctx_batch, config_.batch_size);
        drained += drain_queue(superstep_pending_, superstep_head_, superstep_batch, config_.batch_size);
        drained += drain_queue(model_pending_, model_head_, model_batch, config_.batch_size);
        drained += drain_queue(tool_pending_, tool_head_, tool_batch, config_.batch_size);
        drained += drain_queue(agent_session_pending_, agent_session_head_, agent_session_batch, config_.batch_size);
        drained += drain_queue(agent_turn_pending_, agent_turn_head_, agent_turn_batch, config_.batch_size);
        drained += drain_queue(agent_tool_pending_, agent_tool_head_, agent_tool_batch, config_.batch_size);
        if (drained >= pending_rows_) {
            pending_rows_ = 0;
        } else {
            pending_rows_ -= drained;
        }
        RecomputeBatchReadyNoLock();
        flush_requested_ = false;
        last_pending_rows_.store(static_cast<uint64_t>(pending_rows_), std::memory_order_relaxed);
    }

    if (!mem_batch.empty()) {
        auto n = client.InsertMemEvents(mem_batch);
        inserted += n;
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!trace_batch.empty()) {
        auto n = client.InsertTraceEvents(trace_batch);
        inserted += n;
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!ctx_batch.empty()) {
        auto n = client.InsertContextRows(ctx_batch);
        inserted += n;
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!superstep_batch.empty()) {
        auto n = client.InsertSupersteps(superstep_batch);
        inserted += n;
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!model_batch.empty()) {
        auto n = client.InsertModelInvocations(model_batch);
        inserted += n;
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!tool_batch.empty()) {
        auto n = client.InsertToolCalls(tool_batch);
        inserted += n;
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!agent_session_batch.empty()) {
        auto n = client.InsertAgentSessions(agent_session_batch);
        inserted += n;
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!agent_turn_batch.empty()) {
        auto n = client.InsertAgentTurns(agent_turn_batch);
        inserted += n;
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    if (!agent_tool_batch.empty()) {
        auto n = client.InsertAgentToolCalls(agent_tool_batch);
        inserted += n;
        inserted_count_.fetch_add(n, std::memory_order_relaxed);
    }
    last_flush_rows_.store(static_cast<uint64_t>(inserted), std::memory_order_relaxed);
    return inserted;
}

void AsyncWriter::FlushThread() {
    std::unique_ptr<Client> client;

    while (!stop_.load(std::memory_order_acquire)) {
        {
            std::unique_lock lock(mu_);
            cv_.wait_for(lock, std::chrono::milliseconds(config_.flush_interval_ms), [this] {
                return stop_.load(std::memory_order_relaxed) || batch_ready_ || flush_requested_;
            });
        }
        wake_count_.fetch_add(1, std::memory_order_relaxed);

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
            auto flush_start = std::chrono::steady_clock::now();
            auto rows = DrainAndInsert(*client);
            auto flush_us = static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - flush_start).count());
            last_flush_us_.store(flush_us, std::memory_order_relaxed);
            total_flush_us_.fetch_add(flush_us, std::memory_order_relaxed);
            store_max(max_flush_us_, flush_us);
            if (rows > 0)
                flush_count_.fetch_add(1, std::memory_order_relaxed);
        } catch (...) {
            error_count_.fetch_add(1, std::memory_order_relaxed);
            // Reset client to force reconnect on next iteration
            client.reset();
        }
    }

    // Final drain on shutdown
    if (client) {
        try {
            auto flush_start = std::chrono::steady_clock::now();
            auto rows = DrainAndInsert(*client);
            auto flush_us = static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - flush_start).count());
            last_flush_us_.store(flush_us, std::memory_order_relaxed);
            total_flush_us_.fetch_add(flush_us, std::memory_order_relaxed);
            store_max(max_flush_us_, flush_us);
            if (rows > 0)
                flush_count_.fetch_add(1, std::memory_order_relaxed);
        } catch (...) {
            // Best effort
        }
    }
}

} // namespace seq::ch
