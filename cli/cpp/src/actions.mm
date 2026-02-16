#include "actions.h"

#include "process.h"
#include "trace.h"
#include "strings.h"

#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <map>
#include <optional>
#include <pwd.h>
#include <type_traits>
#include <unistd.h>

#if __has_include(<bit>)
#include <bit>
#endif

// Taskflow currently uses std::bit_ceil/bit_width unconditionally. Some CLT/libc++
// combos don't provide these yet, so polyfill them in a narrow way.
#if !defined(__cpp_lib_bitops)
namespace std {
template <class T>
constexpr T bit_ceil(T x) {
  static_assert(std::is_unsigned_v<T>, "bit_ceil polyfill only supports unsigned");
  if (x <= 1) return 1;
  --x;
  for (size_t shift = 1; shift < sizeof(T) * 8; shift <<= 1) {
    x |= (x >> shift);
  }
  return x + 1;
}

template <class T>
constexpr int bit_width(T x) {
  static_assert(std::is_unsigned_v<T>, "bit_width polyfill only supports unsigned");
  int w = 0;
  while (x) {
    ++w;
    x >>= 1;
  }
  return w;
}
}  // namespace std
#endif

#if __has_include(<taskflow/taskflow.hpp>)
#include <taskflow/taskflow.hpp>
#define SEQ_HAVE_TASKFLOW 1
#else
#define SEQ_HAVE_TASKFLOW 0
#endif

#include <mutex>
#include <thread>
#include <unordered_map>
#include <vector>

#import <ApplicationServices/ApplicationServices.h>
#import <Carbon/Carbon.h>
#import <Cocoa/Cocoa.h>

namespace actions {
namespace {
struct AppCacheEntry {
  std::string name;
  std::string bundle_id;
  std::string app_path;
  pid_t pid = 0;
};

struct AppCache {
  std::mutex mu;
  std::unordered_map<std::string, AppCacheEntry> entries;
};

AppCache& app_cache() {
  static AppCache cache;
  return cache;
}

Result switch_window_or_app_impl();

std::string home_dir() {
  const char* home = ::getenv("HOME");
  if (home && *home) {
    return std::string(home);
  }
  // Background processes (hotkey helpers, daemons) sometimes run without HOME set.
  struct passwd pwd{};
  struct passwd* result = nullptr;
  char buf[16384];
  if (::getpwuid_r(::getuid(), &pwd, buf, sizeof(buf), &result) == 0 && result &&
      result->pw_dir && *result->pw_dir) {
    return std::string(result->pw_dir);
  }
  return std::string();
}

std::string expand_user_path(std::string_view s) {
  if (s.size() >= 2 && s[0] == '~' && s[1] == '/') {
    std::string home = home_dir();
    if (!home.empty()) {
      return home + std::string(s.substr(1));
    }
  }
  return std::string(s);
}

AppCacheEntry get_cache_snapshot(const std::string& name) {
  auto& cache = app_cache();
  std::lock_guard<std::mutex> lock(cache.mu);
  auto it = cache.entries.find(name);
  if (it != cache.entries.end()) {
    return it->second;
  }
  auto [inserted, _] = cache.entries.emplace(name, AppCacheEntry{name, "", "", 0});
  return inserted->second;
}

void update_cache(const std::string& name,
                  const std::string& bundle_id,
                  const std::string& app_path,
                  pid_t pid = 0) {
  auto& cache = app_cache();
  std::lock_guard<std::mutex> lock(cache.mu);
  auto [it, inserted] = cache.entries.try_emplace(name, AppCacheEntry{name, bundle_id, app_path, pid});
  if (!inserted) {
    if (!bundle_id.empty()) {
      it->second.bundle_id = bundle_id;
    }
    if (!app_path.empty()) {
      it->second.app_path = app_path;
    }
    if (pid > 0) {
      it->second.pid = pid;
    }
  }
}

void update_cache_from_running_app(const std::string& name, NSRunningApplication* app) {
  if (!app) return;
  std::string bundle_id;
  std::string app_path;
  if (app.bundleIdentifier) {
    bundle_id = std::string([app.bundleIdentifier UTF8String]);
  }
  if (app.bundleURL && app.bundleURL.path) {
    app_path = std::string([app.bundleURL.path UTF8String]);
  }
  update_cache(name, bundle_id, app_path, app.processIdentifier);
}

uint64_t now_steady_us() {
  using namespace std::chrono;
  return static_cast<uint64_t>(duration_cast<microseconds>(
                                   steady_clock::now().time_since_epoch())
                                   .count());
}

bool open_action_stage_trace_enabled() {
  static int enabled = [] {
    const char* env = std::getenv("SEQ_ACTION_STAGE_TRACE");
    if (!env || !*env) return 0;
    return std::atoi(env) != 0 ? 1 : 0;
  }();
  return enabled != 0;
}

void trace_open_stage(std::string_view event,
                      const std::string& target,
                      std::string_view decision,
                      bool ok,
                      uint64_t total_us,
                      uint64_t front_check_us,
                      uint64_t pid_lookup_us,
                      uint64_t bundle_lookup_us,
                      uint64_t scan_lookup_us,
                      uint64_t activate_us,
                      uint64_t launch_us) {
  if (!open_action_stage_trace_enabled()) return;
  thread_local std::string detail;
  detail.clear();
  detail.reserve(target.size() + decision.size() + 220);
  detail.append("target=").append(target);
  detail.append("\tdecision=").append(decision.data(), decision.size());
  detail.append("\tok=").append(ok ? "1" : "0");
  detail.append("\ttotal_us=").append(std::to_string(total_us));
  detail.append("\tfront_check_us=").append(std::to_string(front_check_us));
  detail.append("\tpid_lookup_us=").append(std::to_string(pid_lookup_us));
  detail.append("\tbundle_lookup_us=").append(std::to_string(bundle_lookup_us));
  detail.append("\tscan_lookup_us=").append(std::to_string(scan_lookup_us));
  detail.append("\tactivate_us=").append(std::to_string(activate_us));
  detail.append("\tlaunch_us=").append(std::to_string(launch_us));
  trace::event(event, detail);
}

void trace_open_with_app_stage(const std::string& app_path,
                               const std::string& file_path,
                               std::string_view decision,
                               bool ok,
                               uint64_t total_us,
                               uint64_t prep_us,
                               uint64_t open_us) {
  if (!open_action_stage_trace_enabled()) return;
  thread_local std::string detail;
  detail.clear();
  detail.reserve(app_path.size() + file_path.size() + decision.size() + 220);
  detail.append("app=").append(app_path);
  detail.append("\tfile=").append(file_path);
  detail.append("\tdecision=").append(decision.data(), decision.size());
  detail.append("\tok=").append(ok ? "1" : "0");
  detail.append("\ttotal_us=").append(std::to_string(total_us));
  detail.append("\tprep_us=").append(std::to_string(prep_us));
  detail.append("\topen_us=").append(std::to_string(open_us));
  trace::event("actions.open_with_app.stage", detail);
}

void post_key(CGKeyCode key, CGEventFlags flags, std::optional<pid_t> pid = std::nullopt) {
  CGEventRef down = CGEventCreateKeyboardEvent(nullptr, key, true);
  CGEventRef up = CGEventCreateKeyboardEvent(nullptr, key, false);
  if (down && up) {
    CGEventSetFlags(down, flags);
    CGEventSetFlags(up, flags);
    if (pid) {
      CGEventPostToPid(*pid, down);
      CGEventPostToPid(*pid, up);
    } else {
      CGEventPost(kCGHIDEventTap, down);
      CGEventPost(kCGHIDEventTap, up);
    }
  }
  if (down) {
    CFRelease(down);
  }
  if (up) {
    CFRelease(up);
  }
}

void post_cmd_key(CGKeyCode key) {
  post_key(key, kCGEventFlagMaskCommand);
}

Result mouse_click_impl(double x, double y) {
  CGPoint pt = CGPointMake(x, y);
  CGEventRef down = CGEventCreateMouseEvent(nullptr, kCGEventLeftMouseDown, pt, kCGMouseButtonLeft);
  CGEventRef up = CGEventCreateMouseEvent(nullptr, kCGEventLeftMouseUp, pt, kCGMouseButtonLeft);
  if (down && up) {
    CGEventPost(kCGHIDEventTap, down);
    CGEventPost(kCGHIDEventTap, up);
  }
  if (down) CFRelease(down);
  if (up) CFRelease(up);
  return {true, ""};
}

Result mouse_double_click_impl(double x, double y) {
  CGPoint pt = CGPointMake(x, y);
  CGEventRef down1 = CGEventCreateMouseEvent(nullptr, kCGEventLeftMouseDown, pt, kCGMouseButtonLeft);
  CGEventRef up1 = CGEventCreateMouseEvent(nullptr, kCGEventLeftMouseUp, pt, kCGMouseButtonLeft);
  CGEventRef down2 = CGEventCreateMouseEvent(nullptr, kCGEventLeftMouseDown, pt, kCGMouseButtonLeft);
  CGEventRef up2 = CGEventCreateMouseEvent(nullptr, kCGEventLeftMouseUp, pt, kCGMouseButtonLeft);
  if (down2) CGEventSetIntegerValueField(down2, kCGMouseEventClickState, 2);
  if (up2) CGEventSetIntegerValueField(up2, kCGMouseEventClickState, 2);
  if (down1) { CGEventPost(kCGHIDEventTap, down1); CFRelease(down1); }
  if (up1) { CGEventPost(kCGHIDEventTap, up1); CFRelease(up1); }
  if (down2) { CGEventPost(kCGHIDEventTap, down2); CFRelease(down2); }
  if (up2) { CGEventPost(kCGHIDEventTap, up2); CFRelease(up2); }
  return {true, ""};
}

Result mouse_right_click_impl(double x, double y) {
  CGPoint pt = CGPointMake(x, y);
  CGEventRef down = CGEventCreateMouseEvent(nullptr, kCGEventRightMouseDown, pt, kCGMouseButtonRight);
  CGEventRef up = CGEventCreateMouseEvent(nullptr, kCGEventRightMouseUp, pt, kCGMouseButtonRight);
  if (down && up) {
    CGEventPost(kCGHIDEventTap, down);
    CGEventPost(kCGHIDEventTap, up);
  }
  if (down) CFRelease(down);
  if (up) CFRelease(up);
  return {true, ""};
}

Result mouse_scroll_impl(double x, double y, int dy) {
  CGPoint pt = CGPointMake(x, y);
  CGEventRef move = CGEventCreateMouseEvent(nullptr, kCGEventMouseMoved, pt, kCGMouseButtonLeft);
  if (move) {
    CGEventPost(kCGHIDEventTap, move);
    CFRelease(move);
  }
  CGEventRef scroll = CGEventCreateScrollWheelEvent(nullptr, kCGScrollEventUnitLine, 1, dy);
  if (scroll) {
    CGEventPost(kCGHIDEventTap, scroll);
    CFRelease(scroll);
  }
  return {true, ""};
}

Result mouse_drag_impl(double x1, double y1, double x2, double y2) {
  CGPoint start = CGPointMake(x1, y1);
  CGPoint end = CGPointMake(x2, y2);
  CGEventRef down = CGEventCreateMouseEvent(nullptr, kCGEventLeftMouseDown, start, kCGMouseButtonLeft);
  if (down) {
    CGEventPost(kCGHIDEventTap, down);
    CFRelease(down);
  }
  usleep(2000);
  CGEventRef drag = CGEventCreateMouseEvent(nullptr, kCGEventLeftMouseDragged, end, kCGMouseButtonLeft);
  if (drag) {
    CGEventPost(kCGHIDEventTap, drag);
    CFRelease(drag);
  }
  usleep(2000);
  CGEventRef up = CGEventCreateMouseEvent(nullptr, kCGEventLeftMouseUp, end, kCGMouseButtonLeft);
  if (up) {
    CGEventPost(kCGHIDEventTap, up);
    CFRelease(up);
  }
  return {true, ""};
}

Result mouse_move_impl(double x, double y) {
  CGPoint pt = CGPointMake(x, y);
  CGEventRef move = CGEventCreateMouseEvent(nullptr, kCGEventMouseMoved, pt, kCGMouseButtonLeft);
  if (move) {
    CGEventPost(kCGHIDEventTap, move);
    CFRelease(move);
  }
  return {true, ""};
}

Result screenshot_impl(const std::string& path) {
  std::string error;
  int code = process::run({"/usr/sbin/screencapture", "-x", "-C", "-t", "png", path}, &error);
  if (code != 0) {
    return {false, "screencapture failed"};
  }
  return {true, ""};
}

bool parse_xy(const std::string& arg, double* x, double* y) {
  char* end1 = nullptr;
  *x = std::strtod(arg.c_str(), &end1);
  if (end1 == arg.c_str()) return false;
  while (*end1 == ' ') ++end1;
  char* end2 = nullptr;
  *y = std::strtod(end1, &end2);
  if (end2 == end1) return false;
  return true;
}

struct ParsedKeystroke {
  CGKeyCode key = (CGKeyCode)0;
  CGEventFlags flags = 0;
  bool ok = false;
};

bool add_modifier(std::string_view token, CGEventFlags* flags) {
  if (token == "cmd" || token == "command") {
    *flags |= kCGEventFlagMaskCommand;
    return true;
  }
  if (token == "ctrl" || token == "control") {
    *flags |= kCGEventFlagMaskControl;
    return true;
  }
  if (token == "opt" || token == "option" || token == "alt") {
    *flags |= kCGEventFlagMaskAlternate;
    return true;
  }
  if (token == "shift") {
    *flags |= kCGEventFlagMaskShift;
    return true;
  }
  return false;
}

std::optional<CGKeyCode> parse_keycode(std::string_view token) {
  if (token.size() == 1) {
    char c = token[0];
    if (c >= 'a' && c <= 'z') {
      return (CGKeyCode)(kVK_ANSI_A + (c - 'a'));
    }
    if (c >= 'A' && c <= 'Z') {
      return (CGKeyCode)(kVK_ANSI_A + (c - 'A'));
    }
    if (c >= '0' && c <= '9') {
      switch (c) {
        case '0':
          return (CGKeyCode)kVK_ANSI_0;
        case '1':
          return (CGKeyCode)kVK_ANSI_1;
        case '2':
          return (CGKeyCode)kVK_ANSI_2;
        case '3':
          return (CGKeyCode)kVK_ANSI_3;
        case '4':
          return (CGKeyCode)kVK_ANSI_4;
        case '5':
          return (CGKeyCode)kVK_ANSI_5;
        case '6':
          return (CGKeyCode)kVK_ANSI_6;
        case '7':
          return (CGKeyCode)kVK_ANSI_7;
        case '8':
          return (CGKeyCode)kVK_ANSI_8;
        case '9':
          return (CGKeyCode)kVK_ANSI_9;
      }
    }
  }

  if (token == "tab") return (CGKeyCode)kVK_Tab;
  if (token == "space") return (CGKeyCode)kVK_Space;
  if (token == "return" || token == "enter") return (CGKeyCode)kVK_Return;
  if (token == "escape" || token == "esc") return (CGKeyCode)kVK_Escape;
  if (token == "grave" || token == "`") return (CGKeyCode)kVK_ANSI_Grave;
  if (token == "semicolon" || token == ";") return (CGKeyCode)kVK_ANSI_Semicolon;
  if (token == "comma" || token == ",") return (CGKeyCode)kVK_ANSI_Comma;
  if (token == "period" || token == ".") return (CGKeyCode)kVK_ANSI_Period;
  if (token == "slash" || token == "/") return (CGKeyCode)kVK_ANSI_Slash;

  return std::nullopt;
}

ParsedKeystroke parse_keystroke(std::string_view spec) {
  ParsedKeystroke out;
  std::string s = strings::trim(spec);
  if (s.empty()) return out;

  auto trim_view = [](std::string_view v) -> std::string_view {
    while (!v.empty()) {
      char c = v.front();
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        v.remove_prefix(1);
        continue;
      }
      break;
    }
    while (!v.empty()) {
      char c = v.back();
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        v.remove_suffix(1);
        continue;
      }
      break;
    }
    return v;
  };

  for (auto& c : s) {
    if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
  }

  CGEventFlags flags = 0;
  std::optional<CGKeyCode> key;

  size_t i = 0;
  while (i < s.size()) {
    size_t j = s.find('+', i);
    std::string_view tok(s.data() + i, (j == std::string::npos ? s.size() : j) - i);
    tok = trim_view(tok);
    if (!tok.empty()) {
      if (add_modifier(tok, &flags)) {
        // ok
      } else if (!key) {
        key = parse_keycode(tok);
        if (!key) return out;
      } else {
        return out;
      }
    }
    if (j == std::string::npos) break;
    i = j + 1;
  }

  if (!key) return out;
  out.key = *key;
  out.flags = flags;
  out.ok = true;
  return out;
}

Result run_keystroke(const macros::Macro& macro) {
  ParsedKeystroke k = parse_keystroke(macro.arg);
  if (!k.ok) {
    return {false, "invalid keystroke"};
  }
  post_key(k.key, k.flags);
  return {true, ""};
}

Result run_keystroke_spec(std::string_view spec, std::optional<pid_t> pid) {
  ParsedKeystroke k = parse_keystroke(spec);
  if (!k.ok) {
    return {false, "invalid keystroke"};
  }
  post_key(k.key, k.flags, pid);
  return {true, ""};
}

Result open_app_impl(std::string_view app, bool toggle) {
  if (app.empty()) {
    return {false, "open_app missing arg"};
  }
  uint64_t total_start_us = now_steady_us();
  uint64_t front_check_us = 0;
  uint64_t pid_lookup_us = 0;
  uint64_t bundle_lookup_us = 0;
  uint64_t scan_lookup_us = 0;
  uint64_t activate_us = 0;
  uint64_t launch_us = 0;

  @autoreleasepool {
    std::string app_str(app);
    NSString* app_name = [NSString stringWithUTF8String:app_str.c_str()];
    if (app_name == nil) {
      trace_open_stage("actions.open_app.stage",
                       app_str,
                       "invalid_utf8",
                       false,
                       now_steady_us() - total_start_us,
                       front_check_us,
                       pid_lookup_us,
                       bundle_lookup_us,
                       scan_lookup_us,
                       activate_us,
                       launch_us);
      return {false, "open_app invalid app name"};
    }

    std::string decision = "unknown";
    auto finish = [&](Result r) -> Result {
      trace_open_stage("actions.open_app.stage",
                       app_str,
                       decision,
                       r.ok,
                       now_steady_us() - total_start_us,
                       front_check_us,
                       pid_lookup_us,
                       bundle_lookup_us,
                       scan_lookup_us,
                       activate_us,
                       launch_us);
      return r;
    };

    auto open_via_ls = [&]() -> Result {
      uint64_t t0 = now_steady_us();
      // `open -a` tends to behave more like "user initiated activation" than
      // NSRunningApplication.activate from a background daemon.
      std::string error;
      int code = process::spawn({"/usr/bin/open", "-a", app_str}, &error);
      launch_us += now_steady_us() - t0;
      if (code != 0) {
        return {false, "open -a failed"};
      }
      return {true, ""};
    };

    AppCacheEntry cached = get_cache_snapshot(app_str);
    NSString* cached_bundle = cached.bundle_id.empty()
                                  ? nil
                                  : [NSString stringWithUTF8String:cached.bundle_id.c_str()];

    uint64_t t_front = now_steady_us();
    NSRunningApplication* front = [[NSWorkspace sharedWorkspace] frontmostApplication];
    bool is_front = false;
    if (cached_bundle && front.bundleIdentifier &&
        [front.bundleIdentifier isEqualToString:cached_bundle]) {
      is_front = true;
    } else {
      NSString* front_name = front.localizedName;
      if (!front_name) {
        front_name = @"";
      }
      if ([front_name isEqualToString:app_name]) {
        is_front = true;
      }
    }
    front_check_us += now_steady_us() - t_front;

    if (is_front) {
      if (toggle) {
        post_cmd_key((CGKeyCode)kVK_Tab);
        decision = "already_front_cmd_tab";
      } else {
        decision = "already_front";
      }
      return finish({true, ""});
    }

    NSRunningApplication* target_app = nil;
    if (cached.pid > 0) {
      uint64_t t_pid = now_steady_us();
      target_app = [NSRunningApplication runningApplicationWithProcessIdentifier:cached.pid];
      pid_lookup_us += now_steady_us() - t_pid;

      if (target_app) {
        if (cached_bundle && target_app.bundleIdentifier &&
            ![target_app.bundleIdentifier isEqualToString:cached_bundle]) {
          target_app = nil;
        } else if (!cached_bundle && target_app.localizedName &&
                   ![target_app.localizedName isEqualToString:app_name]) {
          target_app = nil;
        }
      }
    }

    if (!target_app && cached_bundle) {
      uint64_t t_bundle = now_steady_us();
      NSArray* matches = [NSRunningApplication runningApplicationsWithBundleIdentifier:cached_bundle];
      if ([matches count] > 0) {
        target_app = matches[0];
      }
      bundle_lookup_us += now_steady_us() - t_bundle;
    }
    if (!target_app) {
      uint64_t t_scan = now_steady_us();
      for (NSRunningApplication* running_app in [[NSWorkspace sharedWorkspace] runningApplications]) {
        if ([running_app.localizedName isEqualToString:app_name]) {
          target_app = running_app;
          update_cache_from_running_app(app_str, running_app);
          break;
        }
      }
      scan_lookup_us += now_steady_us() - t_scan;
    }

    if (target_app) {
      update_cache_from_running_app(app_str, target_app);
      // Use IgnoringOtherApps for reliable activation from background daemon context.
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
      NSApplicationActivationOptions opts =
          (NSApplicationActivationOptions)(NSApplicationActivateAllWindows |
                                          NSApplicationActivateIgnoringOtherApps);
#pragma clang diagnostic pop
      uint64_t t_activate = now_steady_us();
      bool ok = [target_app activateWithOptions:opts];
      activate_us += now_steady_us() - t_activate;
      if (ok) {
        // Trust the activation result. Polling is_frontmost() adds latency and
        // can race with WindowServer compositing. The app observer tracks
        // actual transitions via NSWorkspaceDidActivateApplicationNotification.
        decision = (cached.pid > 0) ? "activate_running_cached_pid" : "activate_running";
        return finish({true, ""});
      }
      // activateWithOptions returned NO — app may have been terminated between
      // the runningApplications query and now. Fall through to launch path.
      decision = "activate_failed";
    }

    // App is not running (or activation failed) — need to launch it.
    // Pre-warm cache with bundle ID so next activation is instant.
    if (!cached_bundle) {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
      NSString* app_path = [[NSWorkspace sharedWorkspace] fullPathForApplication:app_name];
#pragma clang diagnostic pop
      if (app_path) {
        NSBundle* bundle = [NSBundle bundleWithPath:app_path];
        if (bundle && bundle.bundleIdentifier) {
          update_cache(app_str,
                       std::string([bundle.bundleIdentifier UTF8String]),
                       std::string([app_path UTF8String]),
                       0);
        }
      }
    }
    // `open -a` is the most reliable way to launch AND activate from a background daemon.
    if (decision != "activate_failed") {
      decision = "launch";
    } else {
      decision = "launch_after_activate_failed";
    }
    return finish(open_via_ls());
  }

  return {true, ""};
}
}  // namespace

Result open_app(std::string_view app) {
  return open_app_impl(app, false);
}

Result open_app_toggle(std::string_view app) {
  return open_app_impl(app, true);
}

Result open_with_app(std::string_view app_path, std::string_view file_path) {
  uint64_t total_start_us = now_steady_us();
  uint64_t prep_us = 0;
  uint64_t open_us = 0;
  std::string app_path_str(app_path);
  std::string file_path_str(file_path);
  if (app_path.empty() || file_path.empty()) {
    trace_open_with_app_stage(app_path_str,
                              file_path_str,
                              "missing_arg",
                              false,
                              now_steady_us() - total_start_us,
                              prep_us,
                              open_us);
    return {false, "open_with_app: missing app_path or file_path"};
  }
  @autoreleasepool {
    uint64_t t_prep = now_steady_us();
    NSString* app_str = [NSString stringWithUTF8String:app_path_str.c_str()];
    NSString* file_str = [NSString stringWithUTF8String:file_path_str.c_str()];
    if (!app_str || !file_str) {
      prep_us += now_steady_us() - t_prep;
      trace_open_with_app_stage(app_path_str,
                                file_path_str,
                                "invalid_utf8",
                                false,
                                now_steady_us() - total_start_us,
                                prep_us,
                                open_us);
      return {false, "open_with_app: invalid UTF-8"};
    }

    NSURL* app_url = [NSURL fileURLWithPath:app_str];
    NSURL* file_url = [NSURL fileURLWithPath:file_str];

    NSWorkspaceOpenConfiguration* config = [NSWorkspaceOpenConfiguration configuration];
    config.activates = YES;
    prep_us += now_steady_us() - t_prep;

    // Synchronous-enough: openURLs dispatches the activation Mach message before returning
    // the completion handler. The app will be activated within one display frame.
    uint64_t t_open = now_steady_us();
    [[NSWorkspace sharedWorkspace] openURLs:@[file_url]
                       withApplicationAtURL:app_url
                              configuration:config
                          completionHandler:nil];
    open_us += now_steady_us() - t_open;
    trace_open_with_app_stage(app_path_str,
                              file_path_str,
                              "open_urls",
                              true,
                              now_steady_us() - total_start_us,
                              prep_us,
                              open_us);
    return {true, ""};
  }
}

Result switch_window_or_app() {
  return switch_window_or_app_impl();
}

Result mouse_click(double x, double y) {
  return mouse_click_impl(x, y);
}

Result mouse_double_click(double x, double y) {
  return mouse_double_click_impl(x, y);
}

Result mouse_right_click(double x, double y) {
  return mouse_right_click_impl(x, y);
}

Result mouse_scroll(double x, double y, int dy) {
  return mouse_scroll_impl(x, y, dy);
}

Result mouse_drag(double x1, double y1, double x2, double y2) {
  return mouse_drag_impl(x1, y1, x2, y2);
}

Result mouse_move(double x, double y) {
  return mouse_move_impl(x, y);
}

Result screenshot(const std::string& path) {
  return screenshot_impl(path);
}

namespace {
std::vector<std::string_view> split_menu_path(std::string_view s) {
  auto trim_view = [](std::string_view v) -> std::string_view {
    while (!v.empty()) {
      char c = v.front();
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        v.remove_prefix(1);
        continue;
      }
      break;
    }
    while (!v.empty()) {
      char c = v.back();
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        v.remove_suffix(1);
        continue;
      }
      break;
    }
    return v;
  };
  s = trim_view(s);
  std::vector<std::string_view> out;
  if (s.empty()) return out;

  auto split_on = [&](char delim) {
    out.clear();
    size_t start = 0;
    while (start <= s.size()) {
      size_t pos = s.find(delim, start);
      if (pos == std::string_view::npos) pos = s.size();
      std::string_view part = s.substr(start, pos - start);
      part = trim_view(part);
      if (!part.empty()) out.push_back(part);
      if (pos == s.size()) break;
      start = pos + 1;
    }
  };

  if (s.find('\t') != std::string_view::npos) {
    split_on('\t');
    return out;
  }
  if (s.find('>') != std::string_view::npos) {
    split_on('>');
    return out;
  }
  if (s.find('/') != std::string_view::npos) {
    split_on('/');
    return out;
  }
  out.push_back(s);
  return out;
}

bool ax_get_title(AXUIElementRef el, std::string* out) {
  if (!el || !out) return false;
  auto try_attr = [&](CFStringRef attr) -> bool {
    CFTypeRef v = nullptr;
    if (AXUIElementCopyAttributeValue(el, attr, &v) != kAXErrorSuccess || !v) {
      return false;
    }
    bool ok = false;
    if (CFGetTypeID(v) == CFStringGetTypeID()) {
      CFStringRef s = (CFStringRef)v;
      char buf[512];
      if (CFStringGetCString(s, buf, sizeof(buf), kCFStringEncodingUTF8)) {
        *out = buf;
        ok = true;
      }
    }
    CFRelease(v);
    return ok;
  };
  if (try_attr(kAXTitleAttribute)) return true;
  // Some menu elements expose their label via description.
  if (try_attr(kAXDescriptionAttribute)) return true;
  return false;
}

AXUIElementRef ax_find_child_by_title(AXUIElementRef parent, std::string_view title) {
  if (!parent) return nullptr;
  CFTypeRef children_any = nullptr;
  if (AXUIElementCopyAttributeValue(parent, kAXChildrenAttribute, &children_any) != kAXErrorSuccess ||
      !children_any) {
    return nullptr;
  }
  AXUIElementRef found = nullptr;
  if (CFGetTypeID(children_any) == CFArrayGetTypeID()) {
    CFArrayRef arr = (CFArrayRef)children_any;
    CFIndex n = CFArrayGetCount(arr);
    for (CFIndex i = 0; i < n; ++i) {
      CFTypeRef child_any = CFArrayGetValueAtIndex(arr, i);
      if (!child_any || CFGetTypeID(child_any) != AXUIElementGetTypeID()) continue;
      AXUIElementRef child = (AXUIElementRef)child_any;
      std::string t;
      if (ax_get_title(child, &t) && t == title) {
        found = child;
        CFRetain(found);
        break;
      }
    }
  }
  CFRelease(children_any);
  return found;
}

AXUIElementRef ax_find_descendant_by_title(AXUIElementRef parent, std::string_view title, int depth) {
  if (!parent || depth <= 0) return nullptr;
  AXUIElementRef direct = ax_find_child_by_title(parent, title);
  if (direct) return direct;
  CFTypeRef children_any = nullptr;
  if (AXUIElementCopyAttributeValue(parent, kAXChildrenAttribute, &children_any) != kAXErrorSuccess ||
      !children_any) {
    return nullptr;
  }
  AXUIElementRef found = nullptr;
  if (CFGetTypeID(children_any) == CFArrayGetTypeID()) {
    CFArrayRef arr = (CFArrayRef)children_any;
    CFIndex n = CFArrayGetCount(arr);
    for (CFIndex i = 0; i < n && !found; ++i) {
      CFTypeRef child_any = CFArrayGetValueAtIndex(arr, i);
      if (!child_any || CFGetTypeID(child_any) != AXUIElementGetTypeID()) continue;
      AXUIElementRef child = (AXUIElementRef)child_any;
      found = ax_find_descendant_by_title(child, title, depth - 1);
    }
  }
  CFRelease(children_any);
  return found;
}
}  // namespace

// Used for debug logging inside menu selection.
std::string frontmost_app_name();

Result select_menu_item(std::string_view app, std::string_view path) {
  if (app.empty()) return {false, "select_menu_item missing app"};
  if (path.empty()) return {false, "select_menu_item missing path"};

  std::string app_str(app);
  std::string path_str(path);
  trace::event("menu.select.start", "app=" + app_str + "\tpath=" + path_str);

  std::optional<pid_t> pid;
  @autoreleasepool {
    NSString* app_name = [NSString stringWithUTF8String:app_str.c_str()];
    if (app_name) {
      for (NSRunningApplication* running_app in [[NSWorkspace sharedWorkspace] runningApplications]) {
        if (running_app.localizedName && [running_app.localizedName isEqualToString:app_name]) {
          pid = running_app.processIdentifier;
          break;
        }
      }
    }
  }
  if (!pid) {
    trace::event("menu.select.err", "no_pid");
    return {false, "select_menu_item app not running"};
  }

  auto parts = split_menu_path(path);
  if (parts.empty()) {
    return {false, "select_menu_item empty path"};
  }

  @autoreleasepool {
    AXUIElementRef app_el = AXUIElementCreateApplication(*pid);
    if (!app_el) return {false, "ax app element failed"};

    CFTypeRef menu_bar_any = nullptr;
    AXError e = AXUIElementCopyAttributeValue(app_el, kAXMenuBarAttribute, &menu_bar_any);
    if (e != kAXErrorSuccess || !menu_bar_any) {
      CFRelease(app_el);
      trace::event(
          "menu.select.err",
          "no_menu_bar\terr=" + std::to_string((int)e) + "\tfront=" + frontmost_app_name());
      return {false, "no menu bar"};
    }
    AXUIElementRef cur = (AXUIElementRef)menu_bar_any;

    // The menu structure is not perfectly uniform across apps. Use a bounded descendant
    // search for the first component from the menu bar, then strict child search for
    // deeper components.
    AXUIElementRef first = ax_find_descendant_by_title(cur, parts[0], 6);
    if (!first) {
      // Debug: emit top-level menu bar item titles.
      std::string tops;
      CFTypeRef bar_children_any = nullptr;
      if (AXUIElementCopyAttributeValue(cur, kAXChildrenAttribute, &bar_children_any) == kAXErrorSuccess &&
          bar_children_any && CFGetTypeID(bar_children_any) == CFArrayGetTypeID()) {
        CFArrayRef arr = (CFArrayRef)bar_children_any;
        CFIndex n = CFArrayGetCount(arr);
        CFIndex limit = n < 18 ? n : 18;
        for (CFIndex i = 0; i < limit; ++i) {
          CFTypeRef child_any = CFArrayGetValueAtIndex(arr, i);
          if (!child_any || CFGetTypeID(child_any) != AXUIElementGetTypeID()) continue;
          AXUIElementRef child = (AXUIElementRef)child_any;
          std::string t;
          if (!ax_get_title(child, &t) || t.empty()) continue;
          if (!tops.empty()) tops.append(",");
          tops.append(t);
        }
      }
      if (bar_children_any) CFRelease(bar_children_any);
      CFRelease(menu_bar_any);
      CFRelease(app_el);
      trace::event("menu.select.err", "missing_part0=" + std::string(parts[0]) + "\ttop=[" + tops + "]");
      return {false, "menu part not found"};
    }
    cur = first;
    for (size_t i = 1; i < parts.size(); ++i) {
      AXUIElementRef next = ax_find_descendant_by_title(cur, parts[i], 6);
      CFRelease(cur);
      if (!next) {
        CFRelease(menu_bar_any);
        CFRelease(app_el);
        trace::event("menu.select.err", "missing_part=" + std::string(parts[i]));
        return {false, "menu part not found"};
      }
      cur = next;
    }

    AXError pe = AXUIElementPerformAction(cur, kAXPressAction);
    CFRelease(cur);
    CFRelease(menu_bar_any);
    CFRelease(app_el);
    if (pe != kAXErrorSuccess) {
      trace::event("menu.select.err", "press_err=" + std::to_string((int)pe));
      return {false, "menu press failed"};
    }
  }
  trace::event("menu.select.ok", "1");
  return {true, ""};
}

std::string frontmost_app_name() {
  @autoreleasepool {
    NSRunningApplication* front = [[NSWorkspace sharedWorkspace] frontmostApplication];
    NSString* name = front.localizedName;
    if (!name) {
      return std::string();
    }
    return std::string([name UTF8String]);
  }
}

FrontmostApp frontmost_app() {
  FrontmostApp out;
  @autoreleasepool {
    NSRunningApplication* front = [[NSWorkspace sharedWorkspace] frontmostApplication];
    if (!front) return out;
    if (front.localizedName) {
      out.name = std::string([front.localizedName UTF8String]);
    }
    if (front.bundleIdentifier) {
      out.bundle_id = std::string([front.bundleIdentifier UTF8String]);
    }
    if (front.bundleURL) {
      out.bundle_url = std::string([[front.bundleURL path] UTF8String]);
    }
    out.pid = front.processIdentifier;
  }
  return out;
}

namespace {
void append_json_string(std::string& out, const std::string& s) {
  out.push_back('"');
  for (char ch : std::string_view(s)) {
    unsigned char c = static_cast<unsigned char>(ch);
    switch (c) {
      case '\\': out.append("\\\\"); break;
      case '"': out.append("\\\""); break;
      case '\n': out.append("\\n"); break;
      case '\r': out.append("\\r"); break;
      case '\t': out.append("\\t"); break;
      default:
        if (c < 0x20) {
          char buf[7];
          std::snprintf(buf, sizeof(buf), "\\u%04x", (unsigned)c);
          out.append(buf);
        } else {
          out.push_back((char)c);
        }
    }
  }
  out.push_back('"');
}
}  // namespace

std::string running_apps_json() {
  @autoreleasepool {
    NSArray* apps = [[NSWorkspace sharedWorkspace] runningApplications];
    std::string out;
    out.reserve(32 * 1024);
    out.append("[");
    bool first = true;
    for (NSRunningApplication* app in apps) {
      if (!app) continue;
      NSString* name = app.localizedName;
      if (!name) continue;

      std::string name_s([name UTF8String]);
      std::string bundle_s;
      if (app.bundleIdentifier) {
        bundle_s = std::string([app.bundleIdentifier UTF8String]);
      }
      std::string bundle_url_s;
      if (app.bundleURL) {
        bundle_url_s = std::string([[app.bundleURL path] UTF8String]);
      }

      if (!first) out.append(",");
      first = false;
      out.append("{\"name\":");
      append_json_string(out, name_s);
      out.append(",\"bundle_id\":");
      append_json_string(out, bundle_s);
      out.append(",\"pid\":").append(std::to_string((int)app.processIdentifier));
      out.append(",\"bundle_url\":");
      append_json_string(out, bundle_url_s);
      out.append("}");
    }
    out.append("]");
    return out;
  }
}

namespace {
Result switch_window_or_app_impl() {
  @autoreleasepool {
    NSRunningApplication* front = [[NSWorkspace sharedWorkspace] frontmostApplication];
    if (!front) {
      return {false, "no front app"};
    }
    if (front.localizedName) {
      trace::event("switch_window_or_app.front_app", [front.localizedName UTF8String]);
    }
    pid_t pid = front.processIdentifier;

    // Prefer AX for counting user-facing windows: CGWindowList can overcount for some apps
    // (multiple backing surfaces), causing false "cycle window" decisions.
    //
    // If AX isn't trusted or fails, fall back to CGWindowList with heuristics.
    CFIndex count = 0;
    bool have_count = false;

    if (AXIsProcessTrusted()) {
      AXUIElementRef app_el = AXUIElementCreateApplication(pid);
      if (app_el) {
        CFTypeRef windows_any = nullptr;
        AXError err = AXUIElementCopyAttributeValue(app_el, kAXWindowsAttribute, &windows_any);
        if (err == kAXErrorSuccess && windows_any && CFGetTypeID(windows_any) == CFArrayGetTypeID()) {
          CFArrayRef windows = static_cast<CFArrayRef>(windows_any);
          CFIndex total = CFArrayGetCount(windows);
          CFIndex titled = 0;
          CFIndex untitled = 0;
          for (CFIndex i = 0; i < total; ++i) {
            AXUIElementRef win = (AXUIElementRef)CFArrayGetValueAtIndex(windows, i);
            if (!win) {
              continue;
            }

            bool is_window = false;
            bool is_standard = false;
            bool big_enough = true;

            CFTypeRef role_any = nullptr;
            if (AXUIElementCopyAttributeValue(win, kAXRoleAttribute, &role_any) == kAXErrorSuccess &&
                role_any && CFGetTypeID(role_any) == CFStringGetTypeID()) {
              is_window = CFStringCompare((CFStringRef)role_any, kAXWindowRole, 0) == kCFCompareEqualTo;
            }
            if (role_any) {
              CFRelease(role_any);
            }

            CFTypeRef subrole_any = nullptr;
            AXError sub_err = AXUIElementCopyAttributeValue(win, kAXSubroleAttribute, &subrole_any);
            if (sub_err == kAXErrorSuccess && subrole_any && CFGetTypeID(subrole_any) == CFStringGetTypeID()) {
              is_standard =
                  CFStringCompare((CFStringRef)subrole_any, kAXStandardWindowSubrole, 0) == kCFCompareEqualTo;
            }
            if (subrole_any) {
              CFRelease(subrole_any);
            }

            if (!is_window) {
              continue;
            }
            if (sub_err == kAXErrorSuccess && !is_standard) {
              continue;
            }

            // Filter out tiny helper windows/panels when the app exposes them via AX.
            CFTypeRef size_any = nullptr;
            if (AXUIElementCopyAttributeValue(win, kAXSizeAttribute, &size_any) == kAXErrorSuccess &&
                size_any && CFGetTypeID(size_any) == AXValueGetTypeID()) {
              CGSize sz{};
              if (AXValueGetValue((AXValueRef)size_any, (AXValueType)kAXValueTypeCGSize, &sz)) {
                if (sz.width < 80.0 || sz.height < 80.0) {
                  big_enough = false;
                }
              }
            }
            if (size_any) {
              CFRelease(size_any);
            }
            if (!big_enough) {
              continue;
            }

            // Heuristic: if at least one window has a title, prefer counting only titled windows.
            // This avoids overcounting backing/helper windows (seen in some apps).
            bool has_title = false;
            CFTypeRef title_any = nullptr;
            if (AXUIElementCopyAttributeValue(win, kAXTitleAttribute, &title_any) == kAXErrorSuccess &&
                title_any && CFGetTypeID(title_any) == CFStringGetTypeID()) {
              has_title = CFStringGetLength((CFStringRef)title_any) > 0;
            }
            if (title_any) {
              CFRelease(title_any);
            }
            if (has_title) {
              ++titled;
            } else {
              ++untitled;
            }
          }
          trace::event("switch_window_or_app.window_count_ax_titled", std::to_string(titled));
          trace::event("switch_window_or_app.window_count_ax_untitled", std::to_string(untitled));
          CFIndex ax_count = (titled > 0) ? titled : (titled + untitled);
          trace::event("switch_window_or_app.window_count_ax", std::to_string(ax_count));
          count = ax_count;
          have_count = true;
        } else {
          trace::event("switch_window_or_app.ax_windows_err", std::to_string((int)err));
        }
        if (windows_any) {
          CFRelease(windows_any);
        }
        CFRelease(app_el);
      }
    } else {
      trace::event("switch_window_or_app.ax_trusted", "0");
    }

    if (!have_count) {
      // CG fallback: count sizable visible layer-0 windows for the front pid.
      // If any windows are named, use only named windows to avoid backing-surface overcount.
      CFArrayRef windows_ref = CGWindowListCopyWindowInfo(
          kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
      if (!windows_ref) {
        trace::event("switch_window_or_app.cg_windows_err", "no window list");
        post_cmd_key((CGKeyCode)kVK_Tab);
        return {true, ""};
      }

      CFIndex named = 0;
      CFIndex unnamed = 0;
      CFIndex total = CFArrayGetCount(windows_ref);
      for (CFIndex i = 0; i < total; ++i) {
        CFDictionaryRef dict = static_cast<CFDictionaryRef>(CFArrayGetValueAtIndex(windows_ref, i));
        if (!dict) {
          continue;
        }
        CFNumberRef owner_pid_ref = static_cast<CFNumberRef>(CFDictionaryGetValue(dict, kCGWindowOwnerPID));
        if (!owner_pid_ref) {
          continue;
        }
        int owner_pid = 0;
        if (!CFNumberGetValue(owner_pid_ref, kCFNumberIntType, &owner_pid)) {
          continue;
        }
        if (owner_pid != pid) {
          continue;
        }

        CFNumberRef layer_ref = static_cast<CFNumberRef>(CFDictionaryGetValue(dict, kCGWindowLayer));
        int layer = 0;
        if (layer_ref && CFNumberGetValue(layer_ref, kCFNumberIntType, &layer)) {
          if (layer != 0) {
            continue;
          }
        }

        CFNumberRef alpha_ref = static_cast<CFNumberRef>(CFDictionaryGetValue(dict, kCGWindowAlpha));
        double alpha = 1.0;
        if (alpha_ref) {
          (void)CFNumberGetValue(alpha_ref, kCFNumberDoubleType, &alpha);
        }
        if (alpha <= 0.01) {
          continue;
        }

        CFDictionaryRef bounds_ref = static_cast<CFDictionaryRef>(CFDictionaryGetValue(dict, kCGWindowBounds));
        CGRect bounds{};
        if (bounds_ref && CGRectMakeWithDictionaryRepresentation(bounds_ref, &bounds)) {
          if (bounds.size.width < 80.0 || bounds.size.height < 80.0) {
            continue;
          }
        }

        CFStringRef name_ref = static_cast<CFStringRef>(CFDictionaryGetValue(dict, kCGWindowName));
        if (name_ref && CFGetTypeID(name_ref) == CFStringGetTypeID() && CFStringGetLength(name_ref) > 0) {
          ++named;
        } else {
          ++unnamed;
        }
      }

      CFRelease(windows_ref);
      trace::event("switch_window_or_app.window_count_cg_named", std::to_string(named));
      trace::event("switch_window_or_app.window_count_cg_unnamed", std::to_string(unnamed));
      count = (named > 0) ? named : (named + unnamed);
    }

    trace::event("switch_window_or_app.window_count", std::to_string(count));

    if (count >= 2) {
      trace::event("switch_window_or_app.result", "cycle_cmd_grave");
      // Post to the front app pid: this behaves more reliably than global posting under
      // some hotkey helpers / TCC setups, and still triggers the standard macOS window cycle.
      post_key((CGKeyCode)kVK_ANSI_Grave, kCGEventFlagMaskCommand, pid);
      return {true, ""};
    }

    trace::event("switch_window_or_app.result", "cmd_tab");
    post_cmd_key((CGKeyCode)kVK_Tab);
    return {true, ""};
  }
}

Result run_open_url(const macros::Macro& macro) {
  if (macro.arg.empty()) {
    return {false, "open_url missing arg"};
  }
  std::vector<std::string> args = {"/usr/bin/open"};
  if (!macro.app.empty()) {
    args.push_back("-a");
    args.push_back(macro.app);
  }
  args.push_back(expand_user_path(macro.arg));
  std::string error;
  // `open` can block for seconds on some apps if we waitpid; for user-facing macros we
  // prefer fire-and-forget for responsiveness.
  trace::event("open_url", std::string(macro.app.empty() ? "" : ("app=" + macro.app + "\t")) + "url=" + macro.arg);
  int code = process::spawn(args, &error);
  if (code != 0) {
    return {false, "open_url failed"};
  }

  // In practice `open -a <App> <URL>` does not always bring the app to the front (Spaces,
  // focus rules, etc). Default to re-activating the app when one is specified.
  if (!macro.app.empty()) {
    const char* env = ::getenv("SEQ_OPEN_URL_ACTIVATE_APP");
    bool activate = true;
    if (env && *env) {
      std::string v(env);
      for (auto& c : v) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
      }
      if (v == "0" || v == "false" || v == "no" || v == "n") {
        activate = false;
      }
    }
    if (activate) {
      actions::Result r = open_app(macro.app);
      trace::event("open_url.activate_app", std::string("app=") + macro.app + "\tok=" + (r.ok ? "1" : "0"));
    }
  }
  return {true, ""};
}

Result run_session_save(const macros::Macro& macro) {
  std::string target = strings::trim(macro.arg);
  for (auto& c : target) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  if (target.empty()) {
    return {false, "session_save missing arg (expected: safari)"};
  }
  if (target != "safari") {
    return {false, "session_save currently supports only safari"};
  }

  // Timestamp for filename + header.
  std::time_t now = std::time(nullptr);
  std::tm tm{};
  localtime_r(&now, &tm);
  char ts_file[32];
  char ts_human[32];
  std::strftime(ts_file, sizeof(ts_file), "%Y-%m-%d_%H%M%S", &tm);
  std::strftime(ts_human, sizeof(ts_human), "%Y-%m-%d %H:%M:%S", &tm);

  std::filesystem::path root = expand_user_path("~/code/nikiv/docs/sessions");
  std::filesystem::path dir = root / target;
  std::error_code ec;
  std::filesystem::create_directories(dir, ec);
  if (ec) {
    return {false, "session_save: failed to create sessions dir"};
  }
  std::filesystem::path out_path = dir / (std::string(ts_file) + ".md");

  // AppleScript: window index, tab index, title, url (tab-separated per line).
  std::vector<std::string> args = {
      "/usr/bin/osascript",
      "-e", "set _out to \"\"",
      "-e", "set _sep to ASCII character 9",
      "-e", "set _nl to ASCII character 10",
      "-e", "tell application \"Safari\"",
      "-e", "  repeat with w in windows",
      "-e", "    set wi to index of w",
      "-e", "    repeat with t in tabs of w",
      "-e", "      set ti to index of t",
      "-e", "      set theURL to URL of t",
      "-e", "      set theTitle to name of t",
      "-e", "      if theURL is not missing value then",
      "-e", "        set _out to _out & wi & _sep & ti & _sep & theTitle & _sep & theURL & _nl",
      "-e", "      end if",
      "-e", "    end repeat",
      "-e", "  end repeat",
      "-e", "end tell",
      "-e", "return _out",
  };
  trace::event("session_save.safari.collect", "start");
  process::CaptureResult cr = process::run_capture(args, {}, "", 10000, 8 * 1024 * 1024);
  if (!cr.ok) {
    trace::event("session_save.safari.collect", std::string("err\tcode=") + std::to_string(cr.exit_code));
    return {false, "session_save: osascript failed (is Safari accessible?)"};
  }

  struct Tab {
    int win = 0;
    int idx = 0;
    std::string title;
    std::string url;
  };

  std::map<int, std::vector<Tab>> by_win;
  size_t i = 0;
  while (i < cr.out.size()) {
    size_t j = cr.out.find('\n', i);
    if (j == std::string::npos) j = cr.out.size();
    std::string line = cr.out.substr(i, j - i);
    i = (j == cr.out.size()) ? j : (j + 1);
    if (line.empty()) continue;

    size_t p1 = line.find('\t');
    size_t p2 = (p1 == std::string::npos) ? std::string::npos : line.find('\t', p1 + 1);
    size_t p3 = (p2 == std::string::npos) ? std::string::npos : line.find('\t', p2 + 1);
    if (p1 == std::string::npos || p2 == std::string::npos || p3 == std::string::npos) {
      continue;
    }
    int win = std::atoi(line.substr(0, p1).c_str());
    int idx = std::atoi(line.substr(p1 + 1, p2 - (p1 + 1)).c_str());
    std::string title = line.substr(p2 + 1, p3 - (p2 + 1));
    std::string url = line.substr(p3 + 1);
    if (url.empty()) continue;

    Tab t;
    t.win = win;
    t.idx = idx;
    t.title = std::move(title);
    t.url = std::move(url);
    by_win[win].push_back(std::move(t));
  }

  // Render markdown (keep it robust: URLs on their own line).
  std::string md;
  md.reserve(4096 + cr.out.size());
  md.append("# Safari session\n\n");
  md.append("Generated: ");
  md.append(ts_human);
  md.append("\n\n");

  size_t total_tabs = 0;
  for (const auto& kv : by_win) total_tabs += kv.second.size();
  md.append("Tabs: ");
  md.append(std::to_string(total_tabs));
  md.append("\n\n");

  for (const auto& kv : by_win) {
    md.append("## Window ");
    md.append(std::to_string(kv.first));
    md.append("\n\n");
    for (const auto& t : kv.second) {
      md.append("- ");
      if (!t.title.empty()) {
        md.append(t.title);
        md.append("\n  ");
      }
      md.append(t.url);
      md.append("\n");
    }
    md.append("\n");
  }

  std::ofstream out(out_path, std::ios::binary);
  if (!out) {
    return {false, "session_save: failed to open output file"};
  }
  out.write(md.data(), (std::streamsize)md.size());
  out.close();
  if (!out) {
    return {false, "session_save: failed to write output file"};
  }

  trace::event("session_save.safari.wrote", out_path.string());
  return {true, ""};
}

Result run_paste_text(const macros::Macro& macro) {
  std::string error;
  int code = process::run_with_input({"/usr/bin/pbcopy"}, macro.arg, &error);
  if (code != 0) {
    return {false, "pbcopy failed"};
  }
  code = process::run(
      {"/usr/bin/osascript",
       "-e",
       "tell application \"System Events\" to keystroke \"v\" using command down"},
      &error);
  if (code != 0) {
    return {false, "paste keystroke failed"};
  }
  return {true, ""};
}

namespace {
bool wait_frontmost(std::string_view expected_app, int timeout_ms) {
  if (expected_app.empty()) {
    return true;
  }

  auto is_frontmost = [&](std::string_view app) -> bool {
    @autoreleasepool {
      NSRunningApplication* front = [[NSWorkspace sharedWorkspace] frontmostApplication];
      if (!front) return false;

      std::string app_str(app);
      NSString* app_name = [NSString stringWithUTF8String:app_str.c_str()];
      if (!app_name) return false;

      AppCacheEntry cached = get_cache_snapshot(app_str);
      NSString* cached_bundle = cached.bundle_id.empty()
                                    ? nil
                                    : [NSString stringWithUTF8String:cached.bundle_id.c_str()];
      if (cached_bundle && front.bundleIdentifier &&
          [front.bundleIdentifier isEqualToString:cached_bundle]) {
        return true;
      }
      if (front.localizedName && [front.localizedName isEqualToString:app_name]) {
        return true;
      }
      return false;
    }
  };

  auto start = std::chrono::steady_clock::now();
  // Fast path: already frontmost.
  if (is_frontmost(expected_app)) {
    return true;
  }
  while (true) {
    auto now = std::chrono::steady_clock::now();
    int elapsed = (int)std::chrono::duration_cast<std::chrono::milliseconds>(now - start).count();
    if (elapsed >= timeout_ms) {
      return false;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    if (is_frontmost(expected_app)) {
      return true;
    }
  }
}

int app_settle_delay_ms() {
  // Some apps (browsers especially) can become "frontmost" before they're ready
  // to receive synthetic keystrokes. Add a tiny settle delay only when the next
  // step is a keystroke to improve reliability while keeping macros fast.
  static int cached = -1;
  if (cached >= 0) return cached;
  const char* v = std::getenv("SEQ_SEQUENCE_APP_SETTLE_MS");
  if (!v || !*v) {
    // Default: small but non-zero; enough to avoid common "activated but not ready"
    // races without making macros feel sluggish.
    cached = 5;
    return cached;
  }
  char* end = nullptr;
  long ms = std::strtol(v, &end, 10);
  if (end == v || ms < 0) {
    cached = 5;
    return cached;
  }
  if (ms > 5000) ms = 5000;
  cached = (int)ms;
  return cached;
}

int wait_frontmost_ms_for_keystrokes() {
  static int cached = -1;
  if (cached >= 0) return cached;
  const char* v = std::getenv("SEQ_SEQUENCE_WAIT_FRONTMOST_MS");
  if (!v || !*v) {
    // Default: low latency, but bounded.
    cached = 250;
    return cached;
  }
  char* end = nullptr;
  long ms = std::strtol(v, &end, 10);
  if (end == v || ms < 0) {
    cached = 250;
    return cached;
  }
  if (ms > 5000) ms = 5000;
  cached = (int)ms;
  return cached;
}

bool eager_keystrokes_enabled() {
  static int cached = -1;
  if (cached >= 0) return cached != 0;
  const char* v = std::getenv("SEQ_SEQUENCE_EAGER_KEYSTROKES");
  if (!v || !*v) {
    cached = 0;
    return false;
  }
  if (v[0] == '1' || v[0] == 'y' || v[0] == 'Y' || v[0] == 't' || v[0] == 'T') {
    cached = 1;
    return true;
  }
  cached = 0;
  return false;
}

std::optional<pid_t> pid_for_app(std::string_view app) {
  if (app.empty()) return std::nullopt;
  @autoreleasepool {
    std::string app_str(app);
    NSString* app_name = [NSString stringWithUTF8String:app_str.c_str()];
    if (app_name == nil) return std::nullopt;

    AppCacheEntry cached = get_cache_snapshot(app_str);
    if (cached.pid > 0) {
      NSRunningApplication* a =
          [NSRunningApplication runningApplicationWithProcessIdentifier:cached.pid];
      if (a) {
        NSString* cached_bundle = cached.bundle_id.empty()
                                      ? nil
                                      : [NSString stringWithUTF8String:cached.bundle_id.c_str()];
        bool pid_matches = false;
        if (cached_bundle && a.bundleIdentifier &&
            [a.bundleIdentifier isEqualToString:cached_bundle]) {
          pid_matches = true;
        } else if (!cached_bundle && a.localizedName &&
                   [a.localizedName isEqualToString:app_name]) {
          pid_matches = true;
        }
        if (pid_matches) {
          update_cache_from_running_app(app_str, a);
          return a.processIdentifier;
        }
      }
    }
    NSString* cached_bundle = cached.bundle_id.empty()
                                  ? nil
                                  : [NSString stringWithUTF8String:cached.bundle_id.c_str()];
    if (cached_bundle) {
      NSArray* matches = [NSRunningApplication runningApplicationsWithBundleIdentifier:cached_bundle];
      if ([matches count] > 0) {
        NSRunningApplication* a = matches[0];
        update_cache_from_running_app(app_str, a);
        return a.processIdentifier;
      }
    }

    for (NSRunningApplication* running_app in [[NSWorkspace sharedWorkspace] runningApplications]) {
      if (running_app.localizedName && [running_app.localizedName isEqualToString:app_name]) {
        update_cache_from_running_app(app_str, running_app);
        return running_app.processIdentifier;
      }
    }
  }
  return std::nullopt;
}

bool parallel_safe(macros::ActionType action) {
  // Start conservative. Most UI-affecting actions are not safe to run concurrently.
  return action == macros::ActionType::OpenUrl;
}

actions::Result run_step_as_macro(const macros::Step& step) {
  macros::Macro tmp;
  tmp.action = step.action;
  tmp.arg = step.arg;
  tmp.app = step.app;
  switch (tmp.action) {
    case macros::ActionType::OpenApp:
      return open_app(tmp.arg);
    case macros::ActionType::OpenAppToggle:
      return open_app_toggle(tmp.arg);
    case macros::ActionType::OpenUrl:
      return run_open_url(tmp);
    case macros::ActionType::SessionSave:
      return run_session_save(tmp);
    case macros::ActionType::PasteText:
      return run_paste_text(tmp);
    case macros::ActionType::SwitchWindowOrApp:
      return switch_window_or_app_impl();
    case macros::ActionType::Keystroke:
      return run_keystroke(tmp);
    case macros::ActionType::SelectMenuItem: {
      if (tmp.app.empty()) {
        return {false, "select_menu_item missing app"};
      }
      return select_menu_item(tmp.app, tmp.arg);
    }
    case macros::ActionType::Click:
    case macros::ActionType::DoubleClick:
    case macros::ActionType::RightClick:
    case macros::ActionType::Scroll:
    case macros::ActionType::Drag:
    case macros::ActionType::MouseMove:
    case macros::ActionType::Screenshot: {
      // Delegate to the main run() which already handles all mouse/screenshot parsing.
      return actions::run(tmp);
    }
    case macros::ActionType::Sequence:
    case macros::ActionType::Todo:
    case macros::ActionType::Unknown:
    default:
      return {false, "invalid step"};
  }
}

actions::Result run_parallel_block(const std::vector<macros::Step>& steps) {
  if (steps.empty()) {
    return {true, ""};
  }
  for (const auto& s : steps) {
    if (!parallel_safe(s.action)) {
      trace::event("seq.sequence.parallel.forced_sequential", macros::action_to_string(s.action));
      // Fallback: run sequentially in caller.
      return {false, "parallel_unsafe"};
    }
  }

#if SEQ_HAVE_TASKFLOW
  static tf::Executor executor;
  tf::Taskflow flow;

  std::vector<actions::Result> results(steps.size());
  for (size_t i = 0; i < steps.size(); ++i) {
    flow.emplace([&, i] { results[i] = run_step_as_macro(steps[i]); });
  }
  executor.run(flow).wait();

  for (const auto& r : results) {
    if (!r.ok) {
      return r;
    }
  }
  return {true, ""};
#else
  // If Taskflow isn't available, run sequentially.
  for (const auto& s : steps) {
    actions::Result r = run_step_as_macro(s);
    if (!r.ok) return r;
  }
  return {true, ""};
#endif
}
}  // namespace

Result run_sequence(const macros::Macro& macro) {
  // When sequences contain keystrokes, targeting them to the app's PID is much
  // faster and more reliable than waiting for macOS to make it frontmost
  // (Spaces/fullscreen activation can take 1s+).
  std::optional<pid_t> keystroke_pid;

  size_t i = 0;
  while (i < macro.steps.size()) {
    const auto& step = macro.steps[i];

    // Run adjacent `parallel: true` steps concurrently when safe.
    if (step.parallel) {
      std::vector<macros::Step> block;
      while (i < macro.steps.size() && macro.steps[i].parallel) {
        block.push_back(macro.steps[i]);
        ++i;
      }
      trace::event("seq.sequence.parallel.start", std::to_string(block.size()));
      actions::Result pr = run_parallel_block(block);
      if (!pr.ok && pr.error == "parallel_unsafe") {
        // One or more steps aren't safe to parallelize; run sequentially.
        for (const auto& s : block) {
          actions::Result r = run_step_as_macro(s);
          if (!r.ok) return r;
        }
      } else {
        if (!pr.ok) return pr;
      }
      continue;
    }

    actions::Result r{true, ""};
    if (step.action == macros::ActionType::Keystroke) {
      r = run_keystroke_spec(step.arg, keystroke_pid);
    } else {
      r = run_step_as_macro(step);
    }
    if (!r.ok) return r;

    // Post-step waits for UI-affecting actions.
    if (step.action == macros::ActionType::OpenApp) {
      bool next_is_key = (i + 1 < macro.steps.size() && macro.steps[i + 1].action == macros::ActionType::Keystroke);

      if (next_is_key && eager_keystrokes_enabled()) {
        // Eager mode: execute the contiguous keystroke block immediately (targeted to PID if we can).
        // This avoids waiting for focus, which can stall for seconds when switching Spaces.
        std::vector<std::string_view> keys;
        size_t j = i + 1;
        while (j < macro.steps.size() && macro.steps[j].action == macros::ActionType::Keystroke) {
          keys.push_back(macro.steps[j].arg);
          ++j;
        }

        // Best-effort: get a PID right away (Arc/Chrome/etc are usually already running).
        std::optional<pid_t> pid = pid_for_app(step.arg);
        std::string front_now = frontmost_app_name();
        if (pid) {
          trace::event("seq.sequence.eager_keystrokes",
                       "mode=pid\tpid=" + std::to_string(*pid) + "\ttarget=" + step.arg + "\tfront=" + front_now +
                           "\tcount=" + std::to_string(keys.size()));
        } else {
          trace::event("seq.sequence.eager_keystrokes",
                       "mode=frontmost\ttarget=" + step.arg + "\tfront=" + front_now + "\tcount=" +
                           std::to_string(keys.size()));
        }

        for (auto spec : keys) {
          actions::Result kr = run_keystroke_spec(spec, pid);
          if (!kr.ok) return kr;
        }

        // Skip the keystroke block we just executed.
        i = j;
        continue;
      }

      if (next_is_key) {
        // Prefer sending keystrokes to the now-frontmost app (KM semantics).
        // Only fall back to PID-targeted injection if focus doesn't land quickly.
        int budget_ms = wait_frontmost_ms_for_keystrokes();
        bool front_ok = wait_frontmost(step.arg, budget_ms);
        std::string front_now = frontmost_app_name();
        if (front_ok) {
          keystroke_pid.reset();
          trace::event("seq.sequence.keystroke_target",
                       "mode=frontmost\ttarget=" + step.arg + "\tfront=" + front_now);
          int ms = app_settle_delay_ms();
          if (ms > 0) {
            trace::event("seq.sequence.app_settle", std::to_string(ms));
            std::this_thread::sleep_for(std::chrono::milliseconds(ms));
          }
        } else {
          keystroke_pid = pid_for_app(step.arg);
          if (keystroke_pid) {
            trace::event("seq.sequence.keystroke_target",
                         "mode=pid\tpid=" + std::to_string(*keystroke_pid) + "\ttarget=" + step.arg +
                             "\tfront=" + front_now);
          } else {
            trace::event("seq.sequence.keystroke_target",
                         "mode=pid\tpid=none\ttarget=" + step.arg + "\tfront=" + front_now);
          }
        }
      } else {
        bool ok = wait_frontmost(step.arg, 1500);
        if (!ok) {
          trace::event("seq.sequence.wait_frontmost_timeout", step.arg);
        }
        keystroke_pid.reset();
      }
    } else if (step.action == macros::ActionType::OpenAppToggle) {
      std::this_thread::sleep_for(std::chrono::milliseconds(120));
      keystroke_pid = pid_for_app(step.arg);
    }
    ++i;
  }
  return {true, ""};
}
}  // namespace

Result run(const macros::Macro& macro) {
  switch (macro.action) {
    case macros::ActionType::OpenApp:
      return open_app(macro.arg);
    case macros::ActionType::OpenAppToggle:
      return open_app_toggle(macro.arg);
    case macros::ActionType::OpenUrl:
      return run_open_url(macro);
    case macros::ActionType::SessionSave:
      return run_session_save(macro);
    case macros::ActionType::PasteText:
      return run_paste_text(macro);
    case macros::ActionType::SwitchWindowOrApp:
      return switch_window_or_app_impl();
    case macros::ActionType::Keystroke:
      return run_keystroke(macro);
    case macros::ActionType::SelectMenuItem: {
      if (macro.app.empty()) return {false, "select_menu_item missing app"};
      return select_menu_item(macro.app, macro.arg);
    }
    case macros::ActionType::Click:
    case macros::ActionType::DoubleClick:
    case macros::ActionType::RightClick:
    case macros::ActionType::Scroll:
    case macros::ActionType::Drag:
    case macros::ActionType::MouseMove: {
      double x = 0, y = 0;
      if (!parse_xy(macro.arg, &x, &y)) {
        return {false, "invalid coordinates"};
      }
      if (macro.action == macros::ActionType::Click) return mouse_click(x, y);
      if (macro.action == macros::ActionType::DoubleClick) return mouse_double_click(x, y);
      if (macro.action == macros::ActionType::RightClick) return mouse_right_click(x, y);
      if (macro.action == macros::ActionType::MouseMove) return mouse_move(x, y);
      if (macro.action == macros::ActionType::Scroll) {
        // arg format: "x y dy"
        char* end1 = nullptr;
        std::strtod(macro.arg.c_str(), &end1);
        while (*end1 == ' ') ++end1;
        char* end2 = nullptr;
        std::strtod(end1, &end2);
        while (*end2 == ' ') ++end2;
        char* end3 = nullptr;
        int dy = (int)std::strtol(end2, &end3, 10);
        if (end3 == end2) return {false, "scroll missing dy"};
        return mouse_scroll(x, y, dy);
      }
      if (macro.action == macros::ActionType::Drag) {
        // arg format: "x1 y1 x2 y2"
        char* end1 = nullptr;
        double x1 = std::strtod(macro.arg.c_str(), &end1);
        while (*end1 == ' ') ++end1;
        char* end2 = nullptr;
        double y1d = std::strtod(end1, &end2);
        while (*end2 == ' ') ++end2;
        char* end3 = nullptr;
        double x2 = std::strtod(end2, &end3);
        while (*end3 == ' ') ++end3;
        char* end4 = nullptr;
        double y2d = std::strtod(end3, &end4);
        if (end4 == end3) return {false, "drag requires x1 y1 x2 y2"};
        return mouse_drag(x1, y1d, x2, y2d);
      }
      return {false, "unknown mouse action"};
    }
    case macros::ActionType::Screenshot:
      return screenshot(macro.arg.empty() ? "/tmp/seq_screenshot.png" : macro.arg);
    case macros::ActionType::Sequence:
      return run_sequence(macro);
    case macros::ActionType::Todo:
      return {false, "macro not implemented"};
    case macros::ActionType::Unknown:
    default:
      return {false, "unknown action"};
  }
}
void prewarm_app_cache() {
  @autoreleasepool {
    NSArray<NSRunningApplication*>* apps = [[NSWorkspace sharedWorkspace] runningApplications];
    for (NSRunningApplication* app in apps) {
      NSString* name = app.localizedName;
      NSString* bundle = app.bundleIdentifier;
      if (!name || !bundle) continue;
      std::string name_str([name UTF8String]);
      std::string bundle_str([bundle UTF8String]);
      std::string path_str;
      if (app.bundleURL && app.bundleURL.path) {
        path_str = std::string([app.bundleURL.path UTF8String]);
      }
      update_cache(name_str, bundle_str, path_str, app.processIdentifier);
    }
  }
}
}  // namespace actions
