#pragma once

#include <cstdint>
#include <string>

namespace context {

struct WindowCtx {
    std::string app_name;
    std::string bundle_id;
    std::string window_title;
    std::string url;
    uint64_t start_ms = 0;
};

// Starts background polling (~1s) of frontmost window context via AX.
// Records "ctx.window" events via metrics::record when context changes.
// Heartbeat: same context → extend duration; change → finalize old + start new.
void start_window_poller();

// Current open window context.
WindowCtx current_window();

// JSON array of last N finalized window context events.
std::string ctx_tail_json(int max_events);

// Starts CGEventTap for input monitoring (timestamps only, never content).
// Records "afk.start" / "afk.end" events when user goes idle / returns.
void start_afk_monitor();

bool is_afk();
uint64_t idle_ms();
std::string afk_status_json();

}  // namespace context
