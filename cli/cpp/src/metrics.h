#pragma once

#include <cstdint>
#include <string_view>
#include <string>

namespace metrics {

// In-process observability "memory engine" backed by Swift + Wax.
//
// All APIs here must:
// - be safe to call from any thread
// - never block the caller (best-effort; drop on any error)

void record(std::string_view name,
            uint64_t ts_ms,
            uint64_t dur_us,
            bool ok,
            std::string_view subject = {});

// Query current in-memory aggregates as JSON.
std::string metrics_json();

// Tail last N recorded events from the in-memory ring buffer as JSON.
std::string tail_json(int max_events);

}  // namespace metrics
