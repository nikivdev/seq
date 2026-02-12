#ifndef SEQ_CLICKHOUSE_BRIDGE_H
#define SEQ_CLICKHOUSE_BRIDGE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct seq_ch_writer seq_ch_writer_t;

seq_ch_writer_t* seq_ch_writer_create(const char* host,
                                       uint16_t port,
                                       const char* database);

void seq_ch_writer_destroy(seq_ch_writer_t* w);

void seq_ch_push_mem_event(seq_ch_writer_t* w,
                           uint64_t ts_ms,
                           uint64_t dur_us,
                           uint8_t ok,
                           const char* session_id,
                           const char* event_id,
                           const char* content_hash,
                           const char* name,
                           const char* subject);  /* NULL for no subject */

void seq_ch_push_trace_event(seq_ch_writer_t* w,
                             int64_t ts_us,
                             const char* app,
                             uint32_t pid,
                             uint64_t tid,
                             const char* level,
                             const char* kind,
                             const char* name,
                             const char* message,
                             int64_t dur_us);

void seq_ch_flush(seq_ch_writer_t* w);

uint64_t seq_ch_error_count(const seq_ch_writer_t* w);
uint64_t seq_ch_inserted_count(const seq_ch_writer_t* w);

/* ── Context events ──────────────────────────────────────────────────────── */

void seq_ch_push_context(seq_ch_writer_t* w,
                          uint64_t ts_ms,
                          uint64_t dur_ms,
                          const char* app,
                          const char* bundle_id,
                          const char* window_title,
                          const char* url,
                          uint8_t afk);

/* ── Hive agent events ───────────────────────────────────────────────────── */

void seq_ch_push_superstep(seq_ch_writer_t* w,
                            uint64_t ts_ms,
                            const char* thread_id,
                            const char* graph_name,
                            uint32_t graph_version,
                            uint32_t step_index,
                            uint32_t frontier_count,
                            uint32_t writes,
                            uint64_t dur_us,
                            uint8_t status);  /* 0=started 1=finished 2=interrupted 3=error */

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
                                   const char* error_msg);

void seq_ch_push_tool_call(seq_ch_writer_t* w,
                            uint64_t ts_ms,
                            const char* thread_id,
                            const char* node_id,
                            const char* tool_name,
                            const char* input_json,
                            const char* output_json,
                            uint64_t dur_us,
                            uint8_t ok);

#ifdef __cplusplus
}
#endif

#endif /* SEQ_CLICKHOUSE_BRIDGE_H */
