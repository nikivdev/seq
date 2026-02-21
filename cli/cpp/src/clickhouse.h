#pragma once

#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace seq::ch {

struct Config {
    std::string host = "127.0.0.1";
    uint16_t port = 9000;
    std::string database = "seq";
    uint32_t batch_size = 4096;
    uint32_t flush_interval_ms = 100;
};

struct MemEventRow {
    uint64_t ts_ms = 0;
    uint64_t dur_us = 0;
    bool ok = true;
    std::string session_id;
    std::string event_id;
    std::string content_hash;
    std::string name;
    std::optional<std::string> subject;
};

struct TraceEventRow {
    int64_t ts_us = 0;
    std::string app;
    uint32_t pid = 0;
    uint64_t tid = 0;
    std::string level;
    std::string kind;
    std::string name;
    std::string message;
    int64_t dur_us = 0;
};

struct ContextRow {
    uint64_t ts_ms = 0;
    uint64_t dur_ms = 0;
    std::string app;
    std::string bundle_id;
    std::string window_title;
    std::string url;
    uint8_t afk = 0;
};

struct SuperstepRow {
    uint64_t ts_ms = 0;
    std::string thread_id;
    std::string graph_name;
    uint32_t graph_version = 0;
    uint32_t step_index = 0;
    uint32_t frontier_count = 0;
    uint32_t writes = 0;
    uint64_t dur_us = 0;
    uint8_t status = 0;
};

struct ModelInvocationRow {
    uint64_t ts_ms = 0;
    std::string thread_id;
    std::string node_id;
    std::string graph_name;
    std::string provider;
    std::string model;
    uint32_t input_tokens = 0;
    uint32_t output_tokens = 0;
    uint64_t dur_us = 0;
    uint64_t ttft_us = 0;
    uint16_t tool_calls = 0;
    uint8_t ok = 1;
    std::string error_msg;
};

struct ToolCallRow {
    uint64_t ts_ms = 0;
    std::string thread_id;
    std::string node_id;
    std::string tool_name;
    std::string input_json;
    std::string output_json;
    uint64_t dur_us = 0;
    uint8_t ok = 1;
};

// ─── Agent (Claude/Codex) observability ─────────────────────────────────────

struct AgentSessionRow {
    uint64_t ts_ms = 0;
    std::string session_id;
    std::string agent; // "claude" or "codex"
    std::string model;
    std::string project_path;
    std::string git_branch;
    std::string git_commit;
    uint64_t dur_ms = 0;
    uint32_t turns = 0;
    uint64_t total_input_tokens = 0;
    uint64_t total_output_tokens = 0;
    double total_cost_usd = 0.0;
};

struct AgentTurnRow {
    uint64_t ts_ms = 0;
    std::string session_id;
    uint32_t turn_index = 0;
    std::string agent;
    std::string model;
    uint32_t input_tokens = 0;
    uint32_t output_tokens = 0;
    uint32_t cached_tokens = 0;
    uint32_t reasoning_tokens = 0;
    uint32_t dur_ms = 0;
    double cost_usd = 0.0;
    std::string stop_reason;
    uint8_t is_error = 0;
    uint32_t context_window = 0;
    float context_used_pct = 0.0f;
};

struct AgentToolCallRow {
    uint64_t ts_ms = 0;
    std::string session_id;
    uint32_t turn_index = 0;
    std::string agent;
    std::string tool_name;
    std::string input_summary;
    uint32_t dur_ms = 0;
    uint8_t ok = 1;
    uint32_t output_lines = 0;
    uint32_t output_bytes = 0;
};

struct AsyncWriterPerfSnapshot {
    uint64_t push_calls = 0;
    uint64_t wake_count = 0;
    uint64_t flush_count = 0;
    uint64_t total_flush_us = 0;
    uint64_t max_flush_us = 0;
    uint64_t last_flush_us = 0;
    uint64_t last_flush_rows = 0;
    uint64_t last_pending_rows = 0;
    uint64_t max_pending_rows = 0;
    uint64_t error_count = 0;
    uint64_t inserted_count = 0;
};

/// Synchronous ClickHouse client using the native binary protocol (port 9000).
/// Wraps clickhouse-cpp with pimpl to hide the dependency from callers.
class Client {
public:
    explicit Client(Config config);
    ~Client();

    Client(const Client&) = delete;
    Client& operator=(const Client&) = delete;

    bool IsAlive() const noexcept;
    size_t InsertMemEvents(std::span<const MemEventRow> rows);
    size_t InsertTraceEvents(std::span<const TraceEventRow> rows);
    size_t InsertContextRows(std::span<const ContextRow> rows);
    size_t InsertSupersteps(std::span<const SuperstepRow> rows);
    size_t InsertModelInvocations(std::span<const ModelInvocationRow> rows);
    size_t InsertToolCalls(std::span<const ToolCallRow> rows);
    size_t InsertAgentSessions(std::span<const AgentSessionRow> rows);
    size_t InsertAgentTurns(std::span<const AgentTurnRow> rows);
    size_t InsertAgentToolCalls(std::span<const AgentToolCallRow> rows);
    void Execute(std::string_view sql);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

/// Async batching writer: lock-protected queues + background flush thread.
/// Push methods are safe to call from any thread. Rows are flushed to ClickHouse
/// every flush_interval_ms or when the batch_size threshold is reached.
class AsyncWriter {
public:
    explicit AsyncWriter(Config config);
    ~AsyncWriter();

    AsyncWriter(const AsyncWriter&) = delete;
    AsyncWriter& operator=(const AsyncWriter&) = delete;

    void PushMemEvent(MemEventRow row) noexcept;
    void PushTraceEvent(TraceEventRow row) noexcept;
    void PushContext(ContextRow row) noexcept;
    void PushSuperstep(SuperstepRow row) noexcept;
    void PushModelInvocation(ModelInvocationRow row) noexcept;
    void PushToolCall(ToolCallRow row) noexcept;
    void PushAgentSession(AgentSessionRow row) noexcept;
    void PushAgentTurn(AgentTurnRow row) noexcept;
    void PushAgentToolCall(AgentToolCallRow row) noexcept;
    void Flush();

    size_t PendingCount() const noexcept;
    uint64_t ErrorCount() const noexcept;
    uint64_t InsertedCount() const noexcept;
    AsyncWriterPerfSnapshot PerfSnapshot() const noexcept;

private:
    void FlushThread();
    size_t DrainAndInsert(Client& client);
    size_t PendingCountNoLock() const noexcept;
    void UpdateMaxPendingNoLock() noexcept;
    void RecomputeBatchReadyNoLock() noexcept;

    Config config_;
    mutable std::mutex mu_;
    std::vector<MemEventRow> mem_pending_;
    std::vector<TraceEventRow> trace_pending_;
    std::vector<ContextRow> ctx_pending_;
    std::vector<SuperstepRow> superstep_pending_;
    std::vector<ModelInvocationRow> model_pending_;
    std::vector<ToolCallRow> tool_pending_;
    std::vector<AgentSessionRow> agent_session_pending_;
    std::vector<AgentTurnRow> agent_turn_pending_;
    std::vector<AgentToolCallRow> agent_tool_pending_;
    size_t mem_head_ = 0;
    size_t trace_head_ = 0;
    size_t ctx_head_ = 0;
    size_t superstep_head_ = 0;
    size_t model_head_ = 0;
    size_t tool_head_ = 0;
    size_t agent_session_head_ = 0;
    size_t agent_turn_head_ = 0;
    size_t agent_tool_head_ = 0;
    size_t pending_rows_ = 0;
    std::vector<MemEventRow> mem_batch_;
    std::vector<TraceEventRow> trace_batch_;
    std::vector<ContextRow> ctx_batch_;
    std::vector<SuperstepRow> superstep_batch_;
    std::vector<ModelInvocationRow> model_batch_;
    std::vector<ToolCallRow> tool_batch_;
    std::vector<AgentSessionRow> agent_session_batch_;
    std::vector<AgentTurnRow> agent_turn_batch_;
    std::vector<AgentToolCallRow> agent_tool_batch_;
    std::condition_variable cv_;
    bool batch_ready_ = false;
    bool flush_requested_ = false;
    std::atomic<bool> stop_{false};
    std::atomic<uint64_t> error_count_{0};
    std::atomic<uint64_t> inserted_count_{0};
    std::atomic<uint64_t> push_count_{0};
    std::atomic<uint64_t> wake_count_{0};
    std::atomic<uint64_t> flush_count_{0};
    std::atomic<uint64_t> total_flush_us_{0};
    std::atomic<uint64_t> max_flush_us_{0};
    std::atomic<uint64_t> last_flush_us_{0};
    std::atomic<uint64_t> last_flush_rows_{0};
    std::atomic<uint64_t> last_pending_rows_{0};
    std::atomic<uint64_t> max_pending_rows_{0};
    std::thread flush_thread_;
};

} // namespace seq::ch
