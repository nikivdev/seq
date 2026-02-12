#include "context.h"
#include "metrics.h"
#include "base.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>

#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>

namespace context {
namespace {

// ---------------------------------------------------------------------------
//  Utilities
// ---------------------------------------------------------------------------

uint64_t now_ms() {
  using namespace std::chrono;
  return (uint64_t)duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

void json_str(std::string& out, const std::string& s) {
  out.push_back('"');
  for (char c : s) {
    switch (c) {
      case '"':  out.append("\\\""); break;
      case '\\': out.append("\\\\"); break;
      case '\n': out.append("\\n");  break;
      case '\r': out.append("\\r");  break;
      case '\t': out.append("\\t");  break;
      default:
        if ((unsigned char)c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", (unsigned char)c);
          out.append(buf);
        } else {
          out.push_back(c);
        }
    }
  }
  out.push_back('"');
}

// ---------------------------------------------------------------------------
//  Window Context
// ---------------------------------------------------------------------------

std::mutex g_ctx_mu;
WindowCtx g_current;

constexpr int kRingCap = 512;
struct FinalizedCtx {
  WindowCtx ctx;
  uint64_t dur_us = 0;
};
FinalizedCtx g_ring[kRingCap];
int g_ring_write = 0;
int g_ring_count = 0;

std::string query_ax_string(AXUIElementRef elem, CFStringRef attr) {
  CFTypeRef val = nullptr;
  AXUIElementCopyAttributeValue(elem, attr, &val);
  if (!val) return {};
  std::string result;
  if (CFGetTypeID(val) == CFStringGetTypeID()) {
    NSString* ns = (__bridge NSString*)val;
    const char* s = [ns UTF8String];
    if (s) result = s;
  } else if (CFGetTypeID(val) == CFURLGetTypeID()) {
    CFStringRef urlStr = CFURLGetString((CFURLRef)val);
    if (urlStr) {
      NSString* ns = (__bridge NSString*)urlStr;
      const char* s = ns ? [ns UTF8String] : nullptr;
      if (s) result = s;
    }
  }
  CFRelease(val);
  return result;
}

std::string query_window_title(pid_t pid) {
  AXUIElementRef app = AXUIElementCreateApplication(pid);
  if (!app) return {};
  std::string title;
  AXUIElementRef window = nullptr;
  AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, (CFTypeRef*)&window);
  if (window) {
    title = query_ax_string(window, kAXTitleAttribute);
    CFRelease(window);
  }
  CFRelease(app);
  return title;
}

std::string query_window_url(pid_t pid) {
  AXUIElementRef app = AXUIElementCreateApplication(pid);
  if (!app) return {};
  std::string url;
  AXUIElementRef window = nullptr;
  AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, (CFTypeRef*)&window);
  if (window) {
    // Try AXDocument (works for editors, some browsers).
    url = query_ax_string(window, kAXDocumentAttribute);
    if (url.empty()) {
      // Try AXURL (some browsers expose this).
      url = query_ax_string(window, CFSTR("AXURL"));
    }
    CFRelease(window);
  }
  CFRelease(app);
  return url;
}

void finalize_current_locked(uint64_t now) {
  if (g_current.app_name.empty() || g_current.start_ms == 0) return;
  uint64_t dur_us = (now - g_current.start_ms) * 1000;

  std::string subject;
  subject.reserve(g_current.window_title.size() + g_current.bundle_id.size() + g_current.url.size() + 4);
  subject.append(g_current.window_title);
  subject.push_back('\t');
  subject.append(g_current.bundle_id);
  subject.push_back('\t');
  subject.append(g_current.url);
  metrics::record("ctx.window", g_current.start_ms, dur_us, true, subject);

  g_ring[g_ring_write] = FinalizedCtx{g_current, dur_us};
  g_ring_write = (g_ring_write + 1) % kRingCap;
  if (g_ring_count < kRingCap) g_ring_count++;
}

void poll_once() {
  @autoreleasepool {
    NSRunningApplication* front = [[NSWorkspace sharedWorkspace] frontmostApplication];
    if (!front) return;

    std::string app = front.localizedName ? std::string([front.localizedName UTF8String]) : "";
    std::string bid = front.bundleIdentifier ? std::string([front.bundleIdentifier UTF8String]) : "";
    pid_t pid = front.processIdentifier;
    if (app.empty()) return;

    std::string title = query_window_title(pid);
    std::string url   = query_window_url(pid);

    uint64_t now = now_ms();
    std::lock_guard<std::mutex> lock(g_ctx_mu);

    if (app   == g_current.app_name &&
        title == g_current.window_title &&
        url   == g_current.url) {
      return;  // unchanged
    }

    finalize_current_locked(now);
    g_current.app_name     = std::move(app);
    g_current.bundle_id    = std::move(bid);
    g_current.window_title = std::move(title);
    g_current.url          = std::move(url);
    g_current.start_ms     = now;
  }
}

// ---------------------------------------------------------------------------
//  AFK Detection
// ---------------------------------------------------------------------------

std::atomic<uint64_t> g_last_input_ms{0};
std::atomic<bool>     g_afk{false};
std::atomic<uint64_t> g_afk_start_ms{0};
constexpr uint64_t kAfkThresholdMs = 5 * 60 * 1000;  // 5 minutes

CGEventRef afk_tap_cb(CGEventTapProxy /*proxy*/, CGEventType /*type*/,
                       CGEventRef event, void* /*ctx*/) {
  g_last_input_ms.store(now_ms(), std::memory_order_relaxed);
  return event;
}

void afk_check_loop() {
  while (true) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
    uint64_t now  = now_ms();
    uint64_t last = g_last_input_ms.load(std::memory_order_relaxed);
    if (last == 0) continue;

    uint64_t idle = now - last;
    bool was_afk  = g_afk.load(std::memory_order_relaxed);

    if (!was_afk && idle >= kAfkThresholdMs) {
      g_afk.store(true, std::memory_order_relaxed);
      g_afk_start_ms.store(last, std::memory_order_relaxed);
      metrics::record("afk.start", last, 0, true);
    } else if (was_afk && idle < kAfkThresholdMs) {
      g_afk.store(false, std::memory_order_relaxed);
      uint64_t start = g_afk_start_ms.load(std::memory_order_relaxed);
      uint64_t dur   = (now - start) * 1000;
      metrics::record("afk.end", start, dur, true);
      g_afk_start_ms.store(0, std::memory_order_relaxed);
    }
  }
}

}  // namespace

// ---------------------------------------------------------------------------
//  Public API
// ---------------------------------------------------------------------------

void start_window_poller() {
  // Poll frontmost window every 1s.
  std::thread([] {
    while (true) {
      poll_once();
      std::this_thread::sleep_for(std::chrono::milliseconds(1000));
    }
  }).detach();

  // Periodic checkpoint every 30s so data isn't lost on crash.
  std::thread([] {
    while (true) {
      std::this_thread::sleep_for(std::chrono::seconds(30));
      uint64_t now = now_ms();
      std::lock_guard<std::mutex> lock(g_ctx_mu);
      if (g_current.app_name.empty() || g_current.start_ms == 0) continue;
      uint64_t dur_us = (now - g_current.start_ms) * 1000;
      std::string subject;
      subject.append(g_current.window_title);
      subject.push_back('\t');
      subject.append(g_current.bundle_id);
      subject.push_back('\t');
      subject.append(g_current.url);
      metrics::record("ctx.window.checkpoint", g_current.start_ms, dur_us, true, subject);
    }
  }).detach();
}

WindowCtx current_window() {
  std::lock_guard<std::mutex> lock(g_ctx_mu);
  return g_current;
}

std::string ctx_tail_json(int max_events) {
  std::lock_guard<std::mutex> lock(g_ctx_mu);
  int n = max_events;
  if (n > g_ring_count) n = g_ring_count;
  if (n <= 0) return "{\"events\":[]}";

  std::string out;
  out.reserve((size_t)n * 256);
  out.append("{\"events\":[");

  int start = (g_ring_write - n + kRingCap) % kRingCap;
  for (int i = 0; i < n; i++) {
    if (i > 0) out.push_back(',');
    const auto& f = g_ring[(start + i) % kRingCap];
    out.append("{\"app\":");
    json_str(out, f.ctx.app_name);
    out.append(",\"title\":");
    json_str(out, f.ctx.window_title);
    out.append(",\"bundle_id\":");
    json_str(out, f.ctx.bundle_id);
    out.append(",\"url\":");
    json_str(out, f.ctx.url);
    out.append(",\"start_ms\":").append(std::to_string(f.ctx.start_ms));
    out.append(",\"dur_us\":").append(std::to_string(f.dur_us));
    out.append("}");
  }
  out.append("]}");
  return out;
}

void start_afk_monitor() {
  g_last_input_ms.store(now_ms(), std::memory_order_relaxed);

  // CGEventTap — listen-only, no content logged.
  std::thread([] {
    CGEventMask mask =
        (1 << kCGEventKeyDown)       |
        (1 << kCGEventMouseMoved)    |
        (1 << kCGEventLeftMouseDown) |
        (1 << kCGEventRightMouseDown)|
        (1 << kCGEventScrollWheel);

    CFMachPortRef tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionListenOnly,
        mask,
        afk_tap_cb,
        nullptr);
    if (!tap) return;  // no permission

    CFRunLoopSourceRef src = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0);
    CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopCommonModes);
    CGEventTapEnable(tap, true);
    CFRunLoopRun();
    CFRelease(src);
    CFRelease(tap);
  }).detach();

  std::thread(afk_check_loop).detach();
}

bool is_afk() {
  return g_afk.load(std::memory_order_relaxed);
}

uint64_t idle_ms() {
  uint64_t last = g_last_input_ms.load(std::memory_order_relaxed);
  if (last == 0) return 0;
  uint64_t now = now_ms();
  return now > last ? now - last : 0;
}

std::string afk_status_json() {
  bool afk    = is_afk();
  uint64_t id = idle_ms();
  std::string out;
  out.reserve(128);
  out.append("{\"afk\":");
  out.append(afk ? "true" : "false");
  out.append(",\"idle_ms\":");
  out.append(std::to_string(id));
  if (afk) {
    out.append(",\"afk_start_ms\":");
    out.append(std::to_string(g_afk_start_ms.load(std::memory_order_relaxed)));
  }
  out.append("}");
  return out;
}

}  // namespace context
