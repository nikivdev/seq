#include "seqd.h"

#include "action_pack_server.h"
#include "actions.h"
#include "base.h"
#include "capture.h"
#include "context.h"
#include "io.h"
#include "macros.h"
#include "strings.h"
#include "trace.h"
#include "metrics.h"

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <cstdio>
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <string>
#include <string_view>
#include <chrono>
#include <cstdlib>
#include <cstdint>
#include <vector>
#include <cstring>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <thread>
#include <pwd.h>
#include <unistd.h>

#import <Cocoa/Cocoa.h>
#include <mach-o/dyld.h>

using std::string_view;

namespace seqd {
namespace {
uint64_t now_epoch_ms() {
  using namespace std::chrono;
  return (uint64_t)duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

uint64_t to_us(std::chrono::steady_clock::duration d) {
  using namespace std::chrono;
  return (uint64_t)duration_cast<microseconds>(d).count();
}

struct AppInfo {
  std::string name;
  std::string bundle_id;
  pid_t pid = 0;
};

std::string home_dir() {
  const char* home = ::getenv("HOME");
  if (home && *home) {
    return std::string(home);
  }
  struct passwd pwd{};
  struct passwd* result = nullptr;
  char buf[16384];
  if (::getpwuid_r(::getuid(), &pwd, buf, sizeof(buf), &result) == 0 && result &&
      result->pw_dir && *result->pw_dir) {
    return std::string(result->pw_dir);
  }
  return std::string();
}

std::filesystem::path seq_config_root() {
  const char* xdg = ::getenv("XDG_CONFIG_HOME");
  if (xdg && *xdg) {
    return std::filesystem::path(xdg) / "seq";
  }
  std::string home = home_dir();
  if (home.empty()) return {};
  return std::filesystem::path(home) / ".config" / "seq";
}

std::string seq_app_support_dir() {
  std::string home = home_dir();
  if (home.empty()) return std::string();
  return home + "/Library/Application Support/seq";
}

std::string action_pack_receiver_conf_path() {
  std::string dir = seq_app_support_dir();
  if (dir.empty()) return std::string();
  return dir + "/action_pack_receiver.conf";
}

bool parse_bool(std::string_view v, bool fallback) {
  if (v == "1" || v == "true" || v == "yes" || v == "on") return true;
  if (v == "0" || v == "false" || v == "no" || v == "off") return false;
  return fallback;
}

size_t parse_size(std::string_view v, size_t fallback) {
  char* end = nullptr;
  errno = 0;
  unsigned long long n = std::strtoull(std::string(v).c_str(), &end, 10);
  if (errno != 0 || !end || *end != '\0') return fallback;
  return static_cast<size_t>(n);
}

int parse_int(std::string_view v, int fallback) {
  char* end = nullptr;
  errno = 0;
  long n = std::strtol(std::string(v).c_str(), &end, 10);
  if (errno != 0 || !end || *end != '\0') return fallback;
  return static_cast<int>(n);
}

bool maybe_load_action_pack_receiver_conf(Options* out) {
  if (!out) return false;
  if (!out->action_pack_listen.empty()) return false;
  std::string path = action_pack_receiver_conf_path();
  if (path.empty()) return false;
  struct stat st{};
  if (::stat(path.c_str(), &st) != 0 || !S_ISREG(st.st_mode)) return false;

  std::ifstream in(path);
  if (!in.good()) return false;

  std::string line;
  while (std::getline(in, line)) {
    std::string trimmed = strings::trim(line);
    if (trimmed.empty()) continue;
    if (!trimmed.empty() && trimmed[0] == '#') continue;
    size_t eq = trimmed.find('=');
    if (eq == std::string::npos || eq == 0) continue;
    std::string k = strings::trim(std::string_view(trimmed).substr(0, eq));
    std::string v = strings::trim(std::string_view(trimmed).substr(eq + 1));
    if (k == "listen") {
      out->action_pack_listen = v;
      continue;
    }
    if (k == "root") {
      out->action_pack_root = v;
      continue;
    }
    if (k == "pubkeys") {
      out->action_pack_pubkeys_path = v;
      continue;
    }
    if (k == "policy") {
      out->action_pack_policy_path = v;
      continue;
    }
    if (k == "allow_local") {
      out->action_pack_allow_local = parse_bool(v, out->action_pack_allow_local);
      continue;
    }
    if (k == "allow_tailscale") {
      out->action_pack_allow_tailscale = parse_bool(v, out->action_pack_allow_tailscale);
      continue;
    }
    if (k == "max_conns") {
      out->action_pack_max_conns = std::max(1, parse_int(v, out->action_pack_max_conns));
      continue;
    }
    if (k == "io_timeout_ms") {
      out->action_pack_io_timeout_ms = std::max(100, parse_int(v, out->action_pack_io_timeout_ms));
      continue;
    }
    if (k == "max_request") {
      out->action_pack_max_request_bytes = parse_size(v, out->action_pack_max_request_bytes);
      continue;
    }
    if (k == "max_output") {
      out->action_pack_max_output_bytes = parse_size(v, out->action_pack_max_output_bytes);
      continue;
    }
  }
  return !out->action_pack_listen.empty();
}

std::vector<std::string> seq_user_app_macro_files() {
  std::vector<std::string> out;
  std::filesystem::path root = seq_config_root();
  if (root.empty()) return out;

  std::filesystem::path apps = root / "apps";
  std::error_code ec;
  if (!std::filesystem::exists(apps, ec) || ec) return out;
  if (!std::filesystem::is_directory(apps, ec) || ec) return out;

  for (const auto& app_dir : std::filesystem::directory_iterator(apps, ec)) {
    if (ec) break;
    if (!app_dir.is_directory(ec) || ec) continue;
    for (const auto& f : std::filesystem::directory_iterator(app_dir.path(), ec)) {
      if (ec) break;
      if (!f.is_regular_file(ec) || ec) continue;
      auto ext = f.path().extension().string();
      std::transform(ext.begin(), ext.end(), ext.begin(), [](unsigned char c) {
        if (c >= 'A' && c <= 'Z') return (char)(c - 'A' + 'a');
        return (char)c;
      });
      if (ext == ".yaml" || ext == ".yml") {
        out.push_back(f.path().string());
      }
    }
  }
  std::sort(out.begin(), out.end());
  return out;
}

void append_seq_user_app_macros(macros::Registry* inout) {
  if (!inout) return;
  for (const auto& path : seq_user_app_macro_files()) {
    std::string e;
    if (!macros::load_append(path, inout, &e)) {
      trace::log("error", "failed to load ~/.config/seq app macros: " + path + "\t" + e);
    } else {
      trace::event("macros.user_app.loaded", path);
    }
  }
}

struct AppState {
  std::mutex mu;
  AppInfo current;
  AppInfo previous;
  uint64_t last_update_ms = 0;
};

AppState& app_state() {
  static AppState state;
  return state;
}

static id g_app_activate_observer_token = nil;
static uint64_t g_start_ms = 0;

std::string executable_path() {
  uint32_t size = 0;
  _NSGetExecutablePath(nullptr, &size);
  if (size == 0) return std::string();
  std::string buf;
  buf.resize(size + 1);
  if (_NSGetExecutablePath(buf.data(), &size) != 0) return std::string();
  buf.resize(std::strlen(buf.c_str()));
  return buf;
}

std::string resolve_seqmem_ch_path() {
  const char* env = std::getenv("SEQ_CH_MEM_PATH");
  if (env && *env) {
    return std::string(env);
  }
  const char* home = std::getenv("HOME");
  std::string home_str;
  if (home && *home) {
    home_str = home;
  } else {
    struct passwd pwd{};
    struct passwd* result = nullptr;
    char buf[16384];
    if (::getpwuid_r(::getuid(), &pwd, buf, sizeof(buf), &result) == 0 && result &&
        result->pw_dir && *result->pw_dir) {
      home_str = result->pw_dir;
    }
  }
  if (home_str.empty()) {
    return std::string();
  }
  std::string path(home_str);
  path += "/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl";
  return path;
}

std::string json_unescape(std::string_view s) {
  std::string out;
  out.reserve(s.size());
  for (size_t i = 0; i < s.size(); ++i) {
    char c = s[i];
    if (c != '\\') {
      out.push_back(c);
      continue;
    }
    if (i + 1 >= s.size()) {
      break;
    }
    char n = s[++i];
    switch (n) {
      case '\\':
        out.push_back('\\');
        break;
      case '"':
        out.push_back('"');
        break;
      case 'n':
        out.push_back('\n');
        break;
      case 'r':
        out.push_back('\r');
        break;
      case 't':
        out.push_back('\t');
        break;
      case 'u': {
        // Minimal \\u00XX support (good enough for our current producers).
        if (i + 4 < s.size() && s[i + 1] == '0' && s[i + 2] == '0') {
          auto hex = [](char h) -> int {
            if (h >= '0' && h <= '9') return h - '0';
            if (h >= 'a' && h <= 'f') return 10 + (h - 'a');
            if (h >= 'A' && h <= 'F') return 10 + (h - 'A');
            return -1;
          };
          int hi = hex(s[i + 3]);
          int lo = hex(s[i + 4]);
          if (hi >= 0 && lo >= 0) {
            out.push_back((char)((hi << 4) | lo));
            i += 4;
            break;
          }
        }
        break;
      }
      default:
        out.push_back(n);
        break;
    }
  }
  return out;
}

void append_json_string(std::string& out, std::string_view value) {
  out.push_back('"');
  for (char raw : value) {
    unsigned char c = static_cast<unsigned char>(raw);
    switch (c) {
      case '"':
        out.append("\\\"");
        break;
      case '\\':
        out.append("\\\\");
        break;
      case '\b':
        out.append("\\b");
        break;
      case '\f':
        out.append("\\f");
        break;
      case '\n':
        out.append("\\n");
        break;
      case '\r':
        out.append("\\r");
        break;
      case '\t':
        out.append("\\t");
        break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out.append(buf);
        } else {
          out.push_back(static_cast<char>(c));
        }
    }
  }
  out.push_back('"');
}

bool extract_json_string_field(std::string_view line,
                              std::string_view key,
                              std::string* out) {
  std::string needle;
  needle.reserve(key.size() + 5);
  needle.append("\"");
  needle.append(key);
  needle.append("\":\"");

  size_t pos = line.find(needle);
  if (pos == std::string_view::npos) {
    return false;
  }
  pos += needle.size();

  std::string_view rest = line.substr(pos);
  for (size_t i = 0; i < rest.size(); ++i) {
    if (rest[i] != '"') {
      continue;
    }
    size_t backslashes = 0;
    size_t j = i;
    while (j > 0 && rest[j - 1] == '\\') {
      ++backslashes;
      --j;
    }
    if ((backslashes % 2) == 0) {
      *out = json_unescape(rest.substr(0, i));
      return true;
    }
  }
  return false;
}

std::string find_previous_app_in_seqmem(std::string_view current) {
  std::string path = resolve_seqmem_ch_path();
  if (path.empty()) {
    return std::string();
  }
  int fd = ::open(path.c_str(), O_RDONLY);
  if (fd < 0) {
    return std::string();
  }

  constexpr off_t kMaxTail = 128 * 1024;
  off_t end = ::lseek(fd, 0, SEEK_END);
  if (end < 0) {
    ::close(fd);
    return std::string();
  }
  off_t start = end > kMaxTail ? end - kMaxTail : 0;
  if (::lseek(fd, start, SEEK_SET) < 0) {
    ::close(fd);
    return std::string();
  }

  std::string buf;
  buf.resize(static_cast<size_t>(end - start));
  ssize_t n = ::read(fd, buf.data(), buf.size());
  ::close(fd);
  if (n <= 0) {
    return std::string();
  }
  buf.resize(static_cast<size_t>(n));

  // Scan backwards by lines; first app.activate subject different from `current` wins.
  //
  // IMPORTANT: handle trailing newlines correctly. A naive `rfind('\n', i-1)` loop can
  // get stuck when `buf` ends with '\n' (common for JSONL).
  size_t end_pos = buf.size();
  while (end_pos > 0 && buf[end_pos - 1] == '\n') {
    --end_pos;
  }
  while (end_pos > 0) {
    size_t nl = buf.rfind('\n', end_pos - 1);
    size_t line_start = (nl == std::string::npos) ? 0 : (nl + 1);
    std::string_view line(buf.data() + line_start, end_pos - line_start);
    if (line.find("\"name\":\"app.activate\"") == std::string_view::npos) {
      if (nl == std::string::npos) {
        break;
      }
      end_pos = nl;
      while (end_pos > 0 && buf[end_pos - 1] == '\n') {
        --end_pos;
      }
      continue;
    }
    std::string subject;
    if (!extract_json_string_field(line, "subject", &subject)) {
      if (nl == std::string::npos) {
        break;
      }
      end_pos = nl;
      while (end_pos > 0 && buf[end_pos - 1] == '\n') {
        --end_pos;
      }
      continue;
    }
    if (!subject.empty() && subject != current) {
      return subject;
    }
    if (nl == std::string::npos) {
      break;
    }
    end_pos = nl;
    while (end_pos > 0 && buf[end_pos - 1] == '\n') {
      --end_pos;
    }
  }
  return std::string();
}

std::string tail_seqmem_ch_as_events_json(int max_events) {
  if (max_events <= 0) {
    return "{\"events\":[]}";
  }
  std::string path = resolve_seqmem_ch_path();
  if (path.empty()) {
    return std::string();
  }
  int fd = ::open(path.c_str(), O_RDONLY);
  if (fd < 0) {
    return std::string();
  }

  constexpr off_t kMaxTail = 512 * 1024;
  off_t end = ::lseek(fd, 0, SEEK_END);
  if (end < 0) {
    ::close(fd);
    return std::string();
  }
  off_t start = end > kMaxTail ? end - kMaxTail : 0;
  if (::lseek(fd, start, SEEK_SET) < 0) {
    ::close(fd);
    return std::string();
  }

  std::string buf;
  buf.resize(static_cast<size_t>(end - start));
  ssize_t n = ::read(fd, buf.data(), buf.size());
  ::close(fd);
  if (n <= 0) {
    return std::string();
  }
  buf.resize(static_cast<size_t>(n));

  std::vector<std::string_view> lines;
  lines.reserve(static_cast<size_t>(max_events));
  size_t line_start = 0;
  while (line_start < buf.size()) {
    size_t nl = buf.find('\n', line_start);
    if (nl == std::string::npos) {
      nl = buf.size();
    }
    std::string_view line(buf.data() + line_start, nl - line_start);
    line_start = nl + 1;
    if (!line.empty()) {
      lines.push_back(line);
    }
  }

  if (lines.empty()) {
    return "{\"events\":[]}";
  }

  int take = max_events;
  if (take > static_cast<int>(lines.size())) {
    take = static_cast<int>(lines.size());
  }
  size_t start_idx = lines.size() - static_cast<size_t>(take);
  std::string out;
  out.reserve(static_cast<size_t>(take) * 200);
  out.append("{\"events\":[");
  for (size_t i = start_idx; i < lines.size(); ++i) {
    if (i != start_idx) {
      out.push_back(',');
    }
    out.append(lines[i].data(), lines[i].size());
  }
  out.append("]}");
  return out;
}

void seed_app_state_from_seqmem() {
  std::string path = resolve_seqmem_ch_path();
  if (path.empty()) {
    return;
  }
  int fd = ::open(path.c_str(), O_RDONLY);
  if (fd < 0) {
    return;
  }

  constexpr off_t kMaxTail = 64 * 1024;
  off_t end = ::lseek(fd, 0, SEEK_END);
  if (end < 0) {
    ::close(fd);
    return;
  }
  off_t start = end > kMaxTail ? end - kMaxTail : 0;
  if (::lseek(fd, start, SEEK_SET) < 0) {
    ::close(fd);
    return;
  }

  std::string buf;
  buf.resize(static_cast<size_t>(end - start));
  ssize_t n = ::read(fd, buf.data(), buf.size());
  ::close(fd);
  if (n <= 0) {
    return;
  }
  buf.resize(static_cast<size_t>(n));

  std::vector<std::string> apps;
  apps.reserve(4);

  size_t line_start = 0;
  while (line_start < buf.size()) {
    size_t nl = buf.find('\n', line_start);
    if (nl == std::string::npos) {
      nl = buf.size();
    }
    std::string_view line(buf.data() + line_start, nl - line_start);
    line_start = nl + 1;

    if (line.empty()) {
      continue;
    }
    if (line.find("\"name\":\"app.activate\"") == std::string_view::npos) {
      continue;
    }
    std::string subject;
    if (!extract_json_string_field(line, "subject", &subject)) {
      continue;
    }
    if (!subject.empty()) {
      apps.push_back(std::move(subject));
    }
  }

  if (apps.empty()) {
    return;
  }

  std::string current = apps.back();
  std::string previous;
  for (int i = (int)apps.size() - 2; i >= 0; --i) {
    if (apps[(size_t)i] != current) {
      previous = apps[(size_t)i];
      break;
    }
  }
  if (current.empty()) {
    return;
  }

  auto& state = app_state();
  std::lock_guard<std::mutex> lock(state.mu);
  if (state.current.name.empty()) {
    state.current.name = current;
  }
  if (!previous.empty() && state.previous.name.empty()) {
    state.previous.name = previous;
  }
}

AppInfo app_info_from(NSRunningApplication* app) {
  AppInfo info;
  if (!app) {
    return info;
  }
  NSString* name = app.localizedName;
  if (name) {
    info.name = std::string([name UTF8String]);
  }
  NSString* bundle = app.bundleIdentifier;
  if (bundle) {
    info.bundle_id = std::string([bundle UTF8String]);
  }
  info.pid = app.processIdentifier;
  return info;
}

bool matches_app_arg(const AppInfo& front, std::string_view arg) {
  if (!front.name.empty() && front.name == arg) {
    return true;
  }
  if (!front.bundle_id.empty() && front.bundle_id == arg) {
    return true;
  }
  return false;
}

bool update_front_app(const AppInfo& info) {
  if (unlikely(info.name.empty())) {
    return false;
  }
  auto& state = app_state();
  std::lock_guard<std::mutex> lock(state.mu);
  state.last_update_ms = now_epoch_ms();
  // Prefer PID when we have it, since localizedName can be ambiguous.
  bool same = false;
  if (state.current.pid != 0 && info.pid != 0) {
    same = state.current.pid == info.pid;
  } else {
    same = state.current.name == info.name;
  }

  if (same) {
    // Upgrade metadata opportunistically (name match but missing pid/bundle_id).
    if (state.current.pid == 0 && info.pid != 0) {
      state.current.pid = info.pid;
    }
    if (state.current.bundle_id.empty() && !info.bundle_id.empty()) {
      state.current.bundle_id = info.bundle_id;
    }
    if (state.current.name.empty() && !info.name.empty()) {
      state.current.name = info.name;
    }
    return false;
  }

  state.previous = state.current;
  state.current = info;
  return true;
}

AppInfo current_front_app() {
  auto& state = app_state();
  std::lock_guard<std::mutex> lock(state.mu);
  return state.current;
}

AppInfo previous_front_app() {
  auto& state = app_state();
  std::lock_guard<std::mutex> lock(state.mu);
  return state.previous;
}

void set_previous_front_app_name_if_empty(std::string_view name) {
  if (name.empty()) {
    return;
  }
  auto& state = app_state();
  std::lock_guard<std::mutex> lock(state.mu);
  if (state.previous.name.empty()) {
    state.previous.name = std::string(name);
  }
}

uint64_t front_app_last_update_ms() {
  auto& state = app_state();
  std::lock_guard<std::mutex> lock(state.mu);
  return state.last_update_ms;
}

bool open_app_force_os_front_query() {
  static int mode = [] {
    const char* env = std::getenv("SEQ_OPEN_APP_FORCE_OS_FRONT_QUERY");
    if (!env || !*env) return 0;
    return std::atoi(env) != 0 ? 1 : 0;
  }();
  return mode != 0;
}

uint64_t open_app_front_cache_max_age_ms() {
  static uint64_t value = [] {
    const char* env = std::getenv("SEQ_OPEN_APP_FRONT_CACHE_MAX_AGE_MS");
    if (!env || !*env) return (uint64_t)120;
    long long parsed = std::atoll(env);
    if (parsed < 0) parsed = 0;
    if (parsed > 10'000) parsed = 10'000;
    return (uint64_t)parsed;
  }();
  return value;
}

bool open_app_allow_seqmem_prev_fallback() {
  static int mode = [] {
    const char* env = std::getenv("SEQ_OPEN_APP_ALLOW_SEQMEM_PREV_FALLBACK");
    if (!env || !*env) return 0;
    return std::atoi(env) != 0 ? 1 : 0;
  }();
  return mode != 0;
}

void start_app_observer() {
  @autoreleasepool {
    NSRunningApplication* front = [[NSWorkspace sharedWorkspace] frontmostApplication];
    update_front_app(app_info_from(front));
  }
  std::thread([] {
    @autoreleasepool {
      NSNotificationCenter* center = [[NSWorkspace sharedWorkspace] notificationCenter];
      // IMPORTANT: retain the observer token, otherwise the observer is removed immediately
      // and we never receive activation notifications.
      g_app_activate_observer_token =
          [center addObserverForName:NSWorkspaceDidActivateApplicationNotification
                              object:nil
                               queue:nil
                          usingBlock:^(NSNotification* note) {
                            NSRunningApplication* app = note.userInfo[NSWorkspaceApplicationKey];
                            AppInfo info = app_info_from(app);
                            bool changed = update_front_app(info);
                            if (changed && !info.name.empty()) {
                              metrics::record("app.activate", now_epoch_ms(), 0, true, info.name);
                            }
                          }];
      [[NSRunLoop currentRunLoop] run];
    }
  }).detach();
}

void start_app_poller() {
  const char* env = std::getenv("SEQ_APP_POLL_MS");
  // Fallback tracking so `open-app-toggle` can reliably switch to the last app
  // even if NSWorkspace activation notifications are missed.
  //
  // Default is intentionally low-frequency to keep CPU usage negligible.
  int poll_ms = 250;
  if (env && *env) {
    poll_ms = std::atoi(env);
  }
  if (poll_ms <= 0) {
    return;  // explicitly disabled
  }
  std::thread([] {
    @autoreleasepool {
      while (true) {
        NSRunningApplication* front = [[NSWorkspace sharedWorkspace] frontmostApplication];
        AppInfo info = app_info_from(front);
        bool changed = update_front_app(info);
        if (changed && !info.name.empty()) {
          metrics::record("app.activate", now_epoch_ms(), 0, true, info.name);
        }
        const char* env2 = std::getenv("SEQ_APP_POLL_MS");
        int ms = 250;
        if (env2 && *env2) {
          ms = std::atoi(env2);
        }
        if (ms <= 0) {
          ms = 250;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(ms));
      }
    }
  }).detach();
}

std::string perf_json() {
  rusage ru{};
  ::getrusage(RUSAGE_SELF, &ru);
  auto tv_us = [](const timeval& tv) -> uint64_t {
    return static_cast<uint64_t>(tv.tv_sec) * 1000000ull + static_cast<uint64_t>(tv.tv_usec);
  };

  uint64_t now_ms = now_epoch_ms();
  uint64_t uptime_ms = (g_start_ms != 0 && now_ms >= g_start_ms) ? (now_ms - g_start_ms) : 0;
  std::string exe = executable_path();

  std::string out;
  out.reserve(384);
  out.append("{\"pid\":").append(std::to_string((int)::getpid()));
  out.append(",\"uptime_ms\":").append(std::to_string(uptime_ms));
  out.append(",\"ax_trusted\":").append(AXIsProcessTrusted() ? "true" : "false");
  out.append(",\"exe\":");
  append_json_string(out, exe);
  out.append(",\"rusage\":{");
  out.append("\"utime_us\":").append(std::to_string(tv_us(ru.ru_utime)));
  out.append(",\"stime_us\":").append(std::to_string(tv_us(ru.ru_stime)));
  out.append(",\"maxrss_kb\":").append(std::to_string((long long)ru.ru_maxrss));
  out.append(",\"nvcsw\":").append(std::to_string((long long)ru.ru_nvcsw));
  out.append(",\"nivcsw\":").append(std::to_string((long long)ru.ru_nivcsw));
  out.append("}");
  out.append("}");
  return out;
}

bool write_all(int fd, const std::string& data) {
  size_t offset = 0;
  while (likely(offset < data.size())) {
    ssize_t n = ::write(fd, data.data() + offset, data.size() - offset);
    if (likely(n > 0)) {
      offset += static_cast<size_t>(n);
      continue;
    }
    if (n < 0 && errno == EINTR) {
      continue;
    }
    return false;
  }
  return true;
}

bool read_line(int fd, std::string* out) {
  out->clear();
  char buf[256];
  while (true) {
    ssize_t n = ::read(fd, buf, sizeof(buf));
    if (likely(n > 0)) {
      for (ssize_t i = 0; i < n; ++i) {
        char c = buf[i];
        if (unlikely(c == '\n')) {
          return true;
        }
        out->push_back(c);
        if (unlikely(out->size() > 4096)) {
          return true;
        }
      }
      continue;
    }
    if (unlikely(n == 0)) {
      return !out->empty();
    }
    if (n < 0 && errno == EINTR) {
      continue;
    }
    return false;
  }
}

std::string_view trim_prefix(std::string_view value, std::string_view prefix) {
  if (strings::starts_with(value, prefix)) {
    value.remove_prefix(prefix.size());
  }
  return value;
}

std::string truncate_for_log(std::string_view value) {
  constexpr size_t kMax = 200;
  if (value.size() <= kMax) {
    return std::string(value);
  }
  std::string out(value.substr(0, kMax));
  out.append("...");
  return out;
}

bool activate_app_fast(const AppInfo& info) {
  // `IgnoringOtherApps` tends to match the "Cmd-Tab" expectation better than
  // only `AllWindows`, especially when another app is currently key.
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
  const NSApplicationActivationOptions kActivateOpts =
      (NSApplicationActivationOptions)(NSApplicationActivateAllWindows |
                                      NSApplicationActivateIgnoringOtherApps);
#pragma clang diagnostic pop
  @autoreleasepool {
    if (info.pid != 0) {
      NSRunningApplication* app = [NSRunningApplication runningApplicationWithProcessIdentifier:info.pid];
      if (app) {
        if ([app activateWithOptions:kActivateOpts]) {
          return true;
        }
      }
    }
    if (!info.bundle_id.empty()) {
      NSString* bundle = [NSString stringWithUTF8String:info.bundle_id.c_str()];
      if (bundle) {
        NSArray* matches = [NSRunningApplication runningApplicationsWithBundleIdentifier:bundle];
        if ([matches count] > 0) {
          NSRunningApplication* app = matches[0];
          if ([app activateWithOptions:kActivateOpts]) {
            return true;
          }
        }
      }
    }
    if (!info.name.empty()) {
      NSString* target = [NSString stringWithUTF8String:info.name.c_str()];
      if (target) {
        NSArray* apps = [[NSWorkspace sharedWorkspace] runningApplications];
        for (NSRunningApplication* app in apps) {
          if (!app) continue;
          NSString* n = app.localizedName;
          if (n && [n isEqualToString:target]) {
            if ([app activateWithOptions:kActivateOpts]) {
              return true;
            }
          }
        }
      }
    }
  }
  return false;
}

std::string handle_open_app_with_state(const std::string& app, bool toggle) {
  AppInfo front;
  AppInfo os_front;
  const char* front_source = "cache";
  uint64_t now_ms = now_epoch_ms();

  // Low-latency fast path: trust the app observer cache when fresh.
  // Fall back to querying NSWorkspace if cache is stale/empty or explicitly forced.
  front = current_front_app();
  uint64_t last_ms = front_app_last_update_ms();
  bool cache_fresh = !front.name.empty() && now_ms >= last_ms &&
                     (now_ms - last_ms) <= open_app_front_cache_max_age_ms();
  if (open_app_force_os_front_query() || !cache_fresh) {
    @autoreleasepool {
      NSRunningApplication* f = [[NSWorkspace sharedWorkspace] frontmostApplication];
      os_front = app_info_from(f);
    }
    if (!os_front.name.empty()) {
      update_front_app(os_front);
      front = os_front;
      front_source = "os";
    }
  }
  if (front.name.empty() && os_front.name.empty()) {
    front_source = "none";
  }

  // Always log toggle inputs in dev; this is the main debugging workflow.
  auto log_diag = [&](std::string_view decision, bool ok) {
    AppInfo cur_dbg = current_front_app();
    AppInfo prev_dbg = previous_front_app();
    uint64_t now_ms = now_epoch_ms();
    uint64_t last_ms = front_app_last_update_ms();
    thread_local std::string subj;
    subj.clear();
    subj.reserve(app.size() + os_front.name.size() + cur_dbg.name.size() + prev_dbg.name.size() + 220);
    subj.append("target=").append(app);
    subj.append("\tos_front=").append(os_front.name);
    subj.append("\tstate_cur=").append(cur_dbg.name);
    subj.append("\tstate_prev=").append(prev_dbg.name);
    subj.append("\tstate_last_update_ms=").append(std::to_string(last_ms));
    subj.append("\tnow_ms=").append(std::to_string(now_ms));
    subj.append("\tfront_source=").append(front_source);
    subj.append("\tdecision=");
    subj.append(decision.data(), decision.size());
    subj.append("\tok=");
    subj.append(ok ? "1" : "0");
    metrics::record("seqd.open_app_toggle.diag", now_epoch_ms(), 0, true, subj);
  };

  if (!front.name.empty() && matches_app_arg(front, app)) {
    if (!toggle) {
      log_diag("already_front", true);
      return "OK\n";
    }
    AppInfo prev = previous_front_app();
    if (!prev.name.empty()) {
      // Prefer direct activation of the last frontmost app (no Cmd-Tab UI, no key injection).
      if (activate_app_fast(prev)) {
        update_front_app(prev);
        log_diag("activate_prev_fast", true);
        return "OK\n";
      }
      if (prev.name != front.name) {
        actions::Result res = actions::open_app(prev.name);
        if (res.ok) {
          update_front_app(prev);
          log_diag("open_prev", true);
          return "OK\n";
        }
      }
    }
    // Optional fallback: scan persisted log for previous app.
    // Disabled by default for low latency (disk IO + parsing in hot path).
    if (open_app_allow_seqmem_prev_fallback()) {
      std::string prev_name = find_previous_app_in_seqmem(front.name);
      if (!prev_name.empty() && prev_name != front.name) {
        AppInfo fallback_prev;
        fallback_prev.name = std::move(prev_name);
        actions::Result res = actions::open_app(fallback_prev.name);
        if (res.ok) {
          update_front_app(fallback_prev);
          log_diag("open_prev_from_log", true);
          return "OK\n";
        }
      }
    }
    actions::Result res = actions::open_app_toggle(app);
    log_diag("cmd_tab_fallback", res.ok);
    return res.ok ? "OK\n" : "ERR " + res.error + "\n";
  }

  actions::Result res = actions::open_app(app);
  if (res.ok) {
    log_diag("open_target", true);
  } else {
    log_diag("open_target", false);
  }
  return res.ok ? "OK\n" : "ERR " + res.error + "\n";
}

std::string app_state_json() {
  AppInfo cur;
  AppInfo prev;
  uint64_t last_ms = 0;
  {
    auto& state = app_state();
    std::lock_guard<std::mutex> lock(state.mu);
    cur = state.current;
    prev = state.previous;
    last_ms = state.last_update_ms;
  }
  std::string out;
  out.reserve(256);
  out.append("{\"current\":");
  out.append("{\"name\":");
  append_json_string(out, cur.name);
  out.append(",\"bundle_id\":");
  append_json_string(out, cur.bundle_id);
  out.append(",\"pid\":").append(std::to_string((int)cur.pid)).append("}");
  out.append(",\"previous\":");
  out.append("{\"name\":");
  append_json_string(out, prev.name);
  out.append(",\"bundle_id\":");
  append_json_string(out, prev.bundle_id);
  out.append(",\"pid\":").append(std::to_string((int)prev.pid)).append("}");
  out.append(",\"last_update_ms\":").append(std::to_string(last_ms));
  out.append("}");
  return out;
}

std::string macros_overlay_path(const std::string& base) {
  std::string local = base;
  auto ends_with = [](const std::string& s, const char* suffix) -> bool {
    size_t n = std::strlen(suffix);
    return s.size() >= n && s.compare(s.size() - n, n, suffix) == 0;
  };
  if (ends_with(local, ".yaml")) {
    local.resize(local.size() - 5);
    local.append(".local.yaml");
  } else {
    local.append(".local.yaml");
  }
  return local;
}

bool reload_macros_registry(const Options& opts, macros::Registry* registry, std::string* out_error) {
  macros::Registry tmp;
  std::string error;
  if (!macros::load(opts.macros, &tmp, &error)) {
    if (out_error) *out_error = std::move(error);
    return false;
  }
  std::string local = macros_overlay_path(opts.macros);
  struct stat st{};
  if (::stat(local.c_str(), &st) == 0 && S_ISREG(st.st_mode)) {
    std::string e2;
    if (!macros::load_append(local, &tmp, &e2)) {
      // Best-effort: keep base macros even if overlay has errors.
      trace::log("error", "failed to load macros overlay: " + e2);
    }
  }
  // Also load user-scoped macros grouped by app:
  //   ~/.config/seq/apps/<app>/*.yaml
  append_seq_user_app_macros(&tmp);
  *registry = std::move(tmp);
  return true;
}

std::string handle_request(const Options& opts, macros::Registry& registry, std::string_view line) {
  TRACE_SCOPE("seqd.handle_request");
  auto t0 = std::chrono::steady_clock::now();
  uint64_t ts_ms = now_epoch_ms();

  std::string trimmed_line = strings::trim(line);
  std::string_view view(trimmed_line);
  trace::event("seqd.request", truncate_for_log(view));

  std::string response;
  std::string event_name = "seqd.unknown";
  std::string subject;
  bool override_ok = false;
  bool ok_value = true;
  bool override_dur = false;
  uint64_t dur_value = 0;

  if (view == "PING") {
    event_name = "seqd.ping";
    response = "PONG\n";
  } else if (view == "PREV_APP") {
    event_name = "seqd.prev_app";
    // Best-effort refresh from OS so "current" is up to date when we compute previous.
    @autoreleasepool {
      NSRunningApplication* f = [[NSWorkspace sharedWorkspace] frontmostApplication];
      AppInfo os_front = app_info_from(f);
      if (!os_front.name.empty()) {
        update_front_app(os_front);
      }
    }
    AppInfo cur = current_front_app();
    AppInfo prev = previous_front_app();
    std::string prev_name = prev.name;
    if (prev_name.empty() || (!cur.name.empty() && prev_name == cur.name)) {
      std::string fallback = find_previous_app_in_seqmem(cur.name);
      if (!fallback.empty() && fallback != cur.name) {
        set_previous_front_app_name_if_empty(fallback);
        prev_name = std::move(fallback);
      }
    }
    response = prev_name;
    response.push_back('\n');
  } else if (view == "APP_STATE") {
    event_name = "seqd.app_state";
    response = app_state_json();
    response.push_back('\n');
  } else if (view == "PERF") {
    event_name = "seqd.perf";
    response = perf_json();
    response.push_back('\n');
  } else if (view == "AX_STATUS") {
    event_name = "seqd.ax_status";
    response = AXIsProcessTrusted() ? "1\n" : "0\n";
  } else if (view == "AX_EXE") {
    event_name = "seqd.ax_exe";
    response = executable_path();
    response.push_back('\n');
  } else if (view == "AX_PROMPT") {
    event_name = "seqd.ax_prompt";
    const void* keys[] = {kAXTrustedCheckOptionPrompt};
    const void* values[] = {kCFBooleanTrue};
    CFDictionaryRef options =
        CFDictionaryCreate(kCFAllocatorDefault,
                           keys,
                           values,
                           1,
                           &kCFCopyStringDictionaryKeyCallBacks,
                           &kCFTypeDictionaryValueCallBacks);
    bool trusted = AXIsProcessTrustedWithOptions(options);
    if (options) {
      CFRelease(options);
    }
    response = trusted ? "OK\n" : "ERR accessibility not trusted\n";
  } else if (view == "MEM_METRICS") {
    event_name = "seqd.mem_metrics";
    response = metrics::metrics_json();
    response.push_back('\n');
  } else if (strings::starts_with(view, "MEM_TAIL ")) {
    event_name = "seqd.mem_tail";
    std::string_view n_view = trim_prefix(view, "MEM_TAIL ");
    std::string n_trim = strings::trim(n_view);
    int n = 50;
    if (!n_trim.empty()) {
      n = std::atoi(n_trim.c_str());
    }
    response = metrics::tail_json(n);
    // After daemon restarts, the in-process ring starts empty; fall back to
    // the shared ClickHouse JSONEachRow log so mem-tail remains useful.
    if (response.find("\"events\":[]") != std::string::npos) {
      std::string fallback = tail_seqmem_ch_as_events_json(n);
      if (!fallback.empty()) {
        response = std::move(fallback);
      }
    }
    response.push_back('\n');
  } else if (strings::starts_with(view, "CTX_TAIL ")) {
    event_name = "seqd.ctx_tail";
    std::string_view n_view = trim_prefix(view, "CTX_TAIL ");
    std::string n_trim = strings::trim(n_view);
    int n = 50;
    if (!n_trim.empty()) n = std::atoi(n_trim.c_str());
    response = context::ctx_tail_json(n);
    response.push_back('\n');
  } else if (view == "AFK_STATUS") {
    event_name = "seqd.afk_status";
    response = context::afk_status_json();
    response.push_back('\n');
  } else if (strings::starts_with(view, "CTX_SEARCH ")) {
    event_name = "seqd.ctx_search";
    std::string_view q_view = trim_prefix(view, "CTX_SEARCH ");
    std::string query = strings::trim(q_view);
    if (query.empty()) {
      response = "ERR usage: CTX_SEARCH <query>\n";
    } else {
      subject = query;
      response = capture::search(query, 20);
      response.push_back('\n');
    }
  } else if (strings::starts_with(view, "TRACE ")) {
    // TRACE ingestion for external tools (e.g. Rise):
    // - Basic:   TRACE <name> <subject>
    // - Tabbed:  TRACE <name>\t<ts_ms>\t<dur_us>\t<ok>\t<subject>
    //
    // Notes:
    // - Input is line-based and capped by read_line() (~4KiB).
    // - We keep parsing minimal and best-effort.
    std::string_view rest_view = trim_prefix(view, "TRACE ");
    std::string rest = strings::trim(rest_view);
    if (rest.empty()) {
      response = "ERR usage: TRACE <name> [subject]\n";
    } else {
      std::string name;
      std::string subject_in;

      auto parse_u64 = [](std::string_view v, uint64_t fallback) -> uint64_t {
        if (v.empty()) return fallback;
        char* end = nullptr;
        unsigned long long x = std::strtoull(std::string(v).c_str(), &end, 10);
        if (!end || end == std::string(v).c_str()) return fallback;
        return static_cast<uint64_t>(x);
      };
      auto parse_ok = [](std::string_view v, bool fallback) -> bool {
        if (v.empty()) return fallback;
        std::string s(v);
        for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        if (s == "1" || s == "true" || s == "ok") return true;
        if (s == "0" || s == "false" || s == "err" || s == "error") return false;
        return fallback;
      };

      // Prefer tab-separated form so subject can contain spaces safely.
      size_t t1 = rest.find('\t');
      if (t1 != std::string::npos) {
        name = strings::trim(std::string_view(rest).substr(0, t1));
        std::string_view rest2 = std::string_view(rest).substr(t1 + 1);
        size_t t2 = rest2.find('\t');
        size_t t3 = t2 == std::string_view::npos ? std::string_view::npos : rest2.find('\t', t2 + 1);
        size_t t4 = t3 == std::string_view::npos ? std::string_view::npos : rest2.find('\t', t3 + 1);
        if (t2 != std::string_view::npos && t3 != std::string_view::npos && t4 != std::string_view::npos) {
          std::string_view ts_s = rest2.substr(0, t2);
          std::string_view dur_s = rest2.substr(t2 + 1, t3 - (t2 + 1));
          std::string_view ok_s = rest2.substr(t3 + 1, t4 - (t3 + 1));
          std::string_view subj_s = rest2.substr(t4 + 1);
          ts_ms = parse_u64(strings::trim(ts_s), ts_ms);
          dur_value = parse_u64(strings::trim(dur_s), 0);
          override_dur = true;
          ok_value = parse_ok(strings::trim(ok_s), true);
          override_ok = true;
          subject_in = std::string(strings::trim(subj_s));
        } else {
          subject_in = std::string(strings::trim(rest2));
        }
      } else {
        size_t sp = rest.find(' ');
        if (sp == std::string::npos) {
          name = strings::trim(std::string_view(rest));
        } else {
          name = strings::trim(std::string_view(rest).substr(0, sp));
          subject_in = std::string(strings::trim(std::string_view(rest).substr(sp + 1)));
        }
      }

      if (name.empty()) {
        response = "ERR usage: TRACE <name> [subject]\n";
      } else {
        // Clamp subject so we don't blow up memory/IO in downstream sinks.
        constexpr size_t kMaxSubject = 3500;
        if (subject_in.size() > kMaxSubject) {
          subject_in.resize(kMaxSubject);
          subject_in.append("...");
        }
        event_name = name;
        subject = std::move(subject_in);
        response = "OK\n";
      }
    }
  } else if (strings::starts_with(view, "INCIDENT_OPEN ")) {
    event_name = "seqd.incident_open";
    std::string_view rest_view = trim_prefix(view, "INCIDENT_OPEN ");
    std::string rest = strings::trim(rest_view);
    size_t sp = rest.find(' ');
    if (rest.empty() || sp == std::string::npos) {
      response = "ERR usage: INCIDENT_OPEN <id> <title>\n";
    } else {
      std::string id = strings::trim(std::string_view(rest).substr(0, sp));
      std::string title = strings::trim(std::string_view(rest).substr(sp + 1));
      if (id.empty() || title.empty()) {
        response = "ERR usage: INCIDENT_OPEN <id> <title>\n";
      } else {
        std::string subj = id;
        subj.push_back('\t');
        subj.append(title);
        metrics::record("incident.open", ts_ms, 0, true, subj);
        response = "OK\n";
      }
    }
  } else if (strings::starts_with(view, "INCIDENT_CLOSE ")) {
    event_name = "seqd.incident_close";
    std::string_view rest_view = trim_prefix(view, "INCIDENT_CLOSE ");
    std::string rest = strings::trim(rest_view);
    if (rest.empty()) {
      response = "ERR usage: INCIDENT_CLOSE <id> [resolution]\n";
    } else {
      size_t sp = rest.find(' ');
      std::string id;
      std::string resolution;
      if (sp == std::string::npos) {
        id = strings::trim(std::string_view(rest));
      } else {
        id = strings::trim(std::string_view(rest).substr(0, sp));
        resolution = strings::trim(std::string_view(rest).substr(sp + 1));
      }
      if (id.empty()) {
        response = "ERR usage: INCIDENT_CLOSE <id> [resolution]\n";
      } else {
        std::string subj = id;
        subj.push_back('\t');
        subj.append(resolution);
        metrics::record("incident.close", ts_ms, 0, true, subj);
        response = "OK\n";
      }
    }
  } else if (strings::starts_with(view, "CLICK ")) {
    event_name = "seqd.click";
    std::string rest = strings::trim(trim_prefix(view, "CLICK "));
    char* end1 = nullptr;
    double x = std::strtod(rest.c_str(), &end1);
    if (end1 == rest.c_str()) {
      response = "ERR usage: CLICK <x> <y>\n";
    } else {
      while (*end1 == ' ') ++end1;
      char* end2 = nullptr;
      double y = std::strtod(end1, &end2);
      if (end2 == end1) {
        response = "ERR usage: CLICK <x> <y>\n";
      } else {
        subject = rest;
        actions::Result r = actions::mouse_click(x, y);
        response = r.ok ? "OK\n" : "ERR " + r.error + "\n";
      }
    }
  } else if (strings::starts_with(view, "DOUBLE_CLICK ")) {
    event_name = "seqd.double_click";
    std::string rest = strings::trim(trim_prefix(view, "DOUBLE_CLICK "));
    char* end1 = nullptr;
    double x = std::strtod(rest.c_str(), &end1);
    if (end1 == rest.c_str()) {
      response = "ERR usage: DOUBLE_CLICK <x> <y>\n";
    } else {
      while (*end1 == ' ') ++end1;
      char* end2 = nullptr;
      double y = std::strtod(end1, &end2);
      if (end2 == end1) {
        response = "ERR usage: DOUBLE_CLICK <x> <y>\n";
      } else {
        subject = rest;
        actions::Result r = actions::mouse_double_click(x, y);
        response = r.ok ? "OK\n" : "ERR " + r.error + "\n";
      }
    }
  } else if (strings::starts_with(view, "RIGHT_CLICK ")) {
    event_name = "seqd.right_click";
    std::string rest = strings::trim(trim_prefix(view, "RIGHT_CLICK "));
    char* end1 = nullptr;
    double x = std::strtod(rest.c_str(), &end1);
    if (end1 == rest.c_str()) {
      response = "ERR usage: RIGHT_CLICK <x> <y>\n";
    } else {
      while (*end1 == ' ') ++end1;
      char* end2 = nullptr;
      double y = std::strtod(end1, &end2);
      if (end2 == end1) {
        response = "ERR usage: RIGHT_CLICK <x> <y>\n";
      } else {
        subject = rest;
        actions::Result r = actions::mouse_right_click(x, y);
        response = r.ok ? "OK\n" : "ERR " + r.error + "\n";
      }
    }
  } else if (strings::starts_with(view, "SCROLL ")) {
    event_name = "seqd.scroll";
    std::string rest = strings::trim(trim_prefix(view, "SCROLL "));
    char* end1 = nullptr;
    double x = std::strtod(rest.c_str(), &end1);
    if (end1 == rest.c_str()) {
      response = "ERR usage: SCROLL <x> <y> <dy>\n";
    } else {
      while (*end1 == ' ') ++end1;
      char* end2 = nullptr;
      double y = std::strtod(end1, &end2);
      if (end2 == end1) {
        response = "ERR usage: SCROLL <x> <y> <dy>\n";
      } else {
        while (*end2 == ' ') ++end2;
        char* end3 = nullptr;
        long dy = std::strtol(end2, &end3, 10);
        if (end3 == end2) {
          response = "ERR usage: SCROLL <x> <y> <dy>\n";
        } else {
          subject = rest;
          actions::Result r = actions::mouse_scroll(x, y, (int)dy);
          response = r.ok ? "OK\n" : "ERR " + r.error + "\n";
        }
      }
    }
  } else if (strings::starts_with(view, "DRAG ")) {
    event_name = "seqd.drag";
    std::string rest = strings::trim(trim_prefix(view, "DRAG "));
    char* end1 = nullptr;
    double x1 = std::strtod(rest.c_str(), &end1);
    if (end1 == rest.c_str()) {
      response = "ERR usage: DRAG <x1> <y1> <x2> <y2>\n";
    } else {
      while (*end1 == ' ') ++end1;
      char* end2 = nullptr;
      double y1 = std::strtod(end1, &end2);
      if (end2 == end1) {
        response = "ERR usage: DRAG <x1> <y1> <x2> <y2>\n";
      } else {
        while (*end2 == ' ') ++end2;
        char* end3 = nullptr;
        double x2 = std::strtod(end2, &end3);
        if (end3 == end2) {
          response = "ERR usage: DRAG <x1> <y1> <x2> <y2>\n";
        } else {
          while (*end3 == ' ') ++end3;
          char* end4 = nullptr;
          double y2 = std::strtod(end3, &end4);
          if (end4 == end3) {
            response = "ERR usage: DRAG <x1> <y1> <x2> <y2>\n";
          } else {
            subject = rest;
            actions::Result r = actions::mouse_drag(x1, y1, x2, y2);
            response = r.ok ? "OK\n" : "ERR " + r.error + "\n";
          }
        }
      }
    }
  } else if (strings::starts_with(view, "MOUSE_MOVE ")) {
    event_name = "seqd.mouse_move";
    std::string rest = strings::trim(trim_prefix(view, "MOUSE_MOVE "));
    char* end1 = nullptr;
    double x = std::strtod(rest.c_str(), &end1);
    if (end1 == rest.c_str()) {
      response = "ERR usage: MOUSE_MOVE <x> <y>\n";
    } else {
      while (*end1 == ' ') ++end1;
      char* end2 = nullptr;
      double y = std::strtod(end1, &end2);
      if (end2 == end1) {
        response = "ERR usage: MOUSE_MOVE <x> <y>\n";
      } else {
        subject = rest;
        actions::Result r = actions::mouse_move(x, y);
        response = r.ok ? "OK\n" : "ERR " + r.error + "\n";
      }
    }
  } else if (strings::starts_with(view, "SCREENSHOT")) {
    event_name = "seqd.screenshot";
    std::string rest = strings::trim(trim_prefix(view, "SCREENSHOT"));
    std::string path = rest.empty() ? "/tmp/seq_screenshot.png" : rest;
    subject = path;
    actions::Result r = actions::screenshot(path);
    if (r.ok) {
      response = path + "\n";
    } else {
      response = "ERR " + r.error + "\n";
    }
  } else if (strings::starts_with(view, "OPEN_WITH_APP ")) {
    event_name = "seqd.open_with_app";
    // Format: OPEN_WITH_APP <app_path>:<file_path>
    std::string_view rest = trim_prefix(view, "OPEN_WITH_APP ");
    std::string arg = strings::trim(rest);
    auto colon = arg.find(':');
    if (colon == std::string::npos || colon == 0 || colon == arg.size() - 1) {
      response = "ERR bad format, expected app_path:file_path\n";
    } else {
      std::string app = arg.substr(0, colon);
      std::string file = arg.substr(colon + 1);
      subject = app + ":" + file;
      trace::event("seqd.open_with_app", subject);
      actions::Result r = actions::open_with_app(app, file);
      response = r.ok ? "OK\n" : "ERR " + r.error + "\n";
    }
  } else if (strings::starts_with(view, "OPEN_APP_TOGGLE ")) {
    event_name = "seqd.open_app_toggle";
    std::string_view name_view = trim_prefix(view, "OPEN_APP_TOGGLE ");
    std::string app = strings::trim(name_view);
    subject = app;
    if (app.empty()) {
      response = "ERR empty app\n";
    } else {
      trace::event("seqd.open_app_toggle", app);
      response = handle_open_app_with_state(app, true);
    }
  } else if (strings::starts_with(view, "RUN ")) {
    event_name = "seqd.run";
    std::string_view macro_view = trim_prefix(view, "RUN ");
    std::string macro_trimmed = strings::trim(macro_view);
    subject = macro_trimmed;
    if (macro_trimmed.empty()) {
      response = "ERR empty macro\n";
    } else {
      trace::event("seqd.run", macro_trimmed);
      const macros::Macro* entry = macros::find(registry, macro_trimmed);
      if (!entry) {
        // Dev ergonomics: if macros were regenerated but seqd wasn't restarted yet,
        // reload once and retry.
        std::string reload_err;
        bool reloaded = reload_macros_registry(opts, &registry, &reload_err);
        trace::event("seqd.macros.reload", reloaded ? "ok" : ("err\t" + reload_err));
        entry = macros::find(registry, macro_trimmed);
      }

      if (!entry) {
        response = "ERR not found\n";
      } else if (entry->action == macros::ActionType::OpenApp ||
                 entry->action == macros::ActionType::OpenAppToggle) {
        bool toggle = entry->action == macros::ActionType::OpenAppToggle;
        response = handle_open_app_with_state(entry->arg, toggle);
      } else {
        actions::Result result = actions::run(*entry);
        if (!result.ok) {
          response = "ERR " + result.error + "\n";
        } else {
          response = "OK\n";
        }
      }
    }
  } else {
    response = "ERR unknown\n";
  }

  uint64_t dur_us = to_us(std::chrono::steady_clock::now() - t0);
  if (override_dur) {
    dur_us = dur_value;
  }
  bool ok = override_ok ? ok_value : (response.rfind("ERR", 0) != 0);

  metrics::record(event_name, ts_ms, dur_us, ok, subject);

  return response;
}

// Fire-and-forget DGRAM listener — no connect/accept overhead.
// Runs on a detached thread alongside the STREAM server.
void run_dgram_listener(const Options& opts, macros::Registry& registry) {
  std::string dgram_path = opts.socket_path + ".dgram";
  ::unlink(dgram_path.c_str());

  int fd = ::socket(AF_UNIX, SOCK_DGRAM, 0);
  if (fd < 0) {
    trace::log("error", "dgram socket failed");
    return;
  }

  sockaddr_un addr{};
  addr.sun_family = AF_UNIX;
  if (dgram_path.size() >= sizeof(addr.sun_path)) {
    trace::log("error", "dgram socket path too long");
    ::close(fd);
    return;
  }
  std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", dgram_path.c_str());

#if defined(__APPLE__)
  socklen_t addrlen =
      static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + std::strlen(addr.sun_path));
  if (addrlen <= 0xff) {
    addr.sun_len = static_cast<uint8_t>(addrlen);
  } else {
    addr.sun_len = static_cast<uint8_t>(sizeof(sockaddr_un));
  }
#else
  socklen_t addrlen =
      static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + std::strlen(addr.sun_path) + 1);
#endif

  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), addrlen) != 0) {
    trace::log("error", "dgram bind failed");
    ::close(fd);
    return;
  }

  // Increase receive buffer for burst tolerance (64KB).
  int bufsize = 65536;
  ::setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bufsize, sizeof(bufsize));

  trace::log("info", "seqd dgram listening on " + dgram_path);

  char buf[4096];
  while (true) {
    ssize_t n = ::recvfrom(fd, buf, sizeof(buf) - 1, 0, nullptr, nullptr);
    if (n < 0) {
      if (errno == EINTR) continue;
      trace::log("error", std::string("dgram recvfrom errno=") + std::to_string(errno));
      break;
    }
    if (n == 0) continue;
    buf[n] = '\0';
    // Strip trailing newline if present.
    if (n > 0 && buf[n - 1] == '\n') buf[--n] = '\0';
    if (n == 0) continue;
    std::string line(buf, static_cast<size_t>(n));
    // Fire-and-forget: process but discard response.
    handle_request(opts, registry, line);
  }
  ::close(fd);
  ::unlink(dgram_path.c_str());
}

int run_server(const Options& opts, macros::Registry& registry) {
  int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd < 0) {
    io::err.write("error: socket failed\n");
    return 1;
  }

  ::unlink(opts.socket_path.c_str());
  sockaddr_un addr{};
  addr.sun_family = AF_UNIX;
  if (opts.socket_path.size() >= sizeof(addr.sun_path)) {
    io::err.write("error: socket path too long\n");
    ::close(fd);
    return 1;
  }
  std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", opts.socket_path.c_str());

  // IMPORTANT (macOS/BSD): the sockaddr_un length must match the actual path length.
  // Passing sizeof(sockaddr_un) can create a non-filesystem socket name with trailing
  // garbage bytes, making the socket path invisible/unconnectable from clients.
  // sockaddr_un length semantics differ:
  // - macOS/BSD: length is offsetof + strlen (no trailing NUL)
  // - Linux: commonly uses offsetof + strlen + 1 (include trailing NUL)
#if defined(__APPLE__)
  socklen_t addrlen =
      static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + std::strlen(addr.sun_path));
#else
  socklen_t addrlen =
      static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + std::strlen(addr.sun_path) + 1);
#endif
#if defined(__APPLE__)
  if (addrlen <= 0xff) {
    addr.sun_len = static_cast<uint8_t>(addrlen);
  } else {
    // Should never happen with normal UNIX socket paths, but keep behavior defined.
    addr.sun_len = static_cast<uint8_t>(sizeof(sockaddr_un));
  }
#endif

  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), addrlen) != 0) {
    io::err.write("error: bind failed\n");
    ::close(fd);
    return 1;
  }
  if (::listen(fd, 16) != 0) {
    io::err.write("error: listen failed\n");
    ::close(fd);
    return 1;
  }

  trace::log("info", "seqd listening");

  // Launch fire-and-forget DGRAM listener on a detached thread.
  std::thread dgram_thread([&opts, &registry] { run_dgram_listener(opts, registry); });
  dgram_thread.detach();

    while (true) {
      int client = ::accept(fd, nullptr, nullptr);
      if (unlikely(client < 0)) {
        if (errno == EINTR) {
          continue;
        }
        trace::log("error", std::string("accept failed errno=") + std::to_string(errno));
        break;
      }
      // Keep connection alive: process multiple newline-delimited commands per client.
      // This supports persistent connections from Karabiner's socket_sender, avoiding
      // connect()/accept() overhead per command (~0.1ms savings per message).
      std::string line;
      while (read_line(client, &line)) {
        if (line.empty()) continue;
        std::string response = handle_request(opts, registry, line);
        if (!write_all(client, response)) {
          break; // Client disconnected.
        }
        line.clear();
      }
      ::close(client);
    }
  ::close(fd);
  return 0;
}
}  // namespace

int run_daemon(const Options& opts) {
  signal(SIGPIPE, SIG_IGN);

  // NSApplication is required for ScreenCaptureKit dispatch callbacks and
  // NSWorkspace notification delivery.
  @autoreleasepool {
    [NSApplication sharedApplication];
  }

  trace::log("info", std::string("seqd start ax=") + (AXIsProcessTrusted() ? "1" : "0"));
  g_start_ms = now_epoch_ms();
  macros::Registry registry;
  std::string error;
  if (!macros::load(opts.macros, &registry, &error)) {
    io::err.write("error: ");
    io::err.write(error);
    io::err.write("\n");
    trace::log("error", error);
    return 1;
  }
  // Optional overlay file for hand-authored/experimental macros that shouldn't be
  // regenerated by tools/gen_macros.py.
  {
    std::string local = opts.macros;
    auto ends_with = [](const std::string& s, const char* suffix) -> bool {
      size_t n = std::strlen(suffix);
      return s.size() >= n && s.compare(s.size() - n, n, suffix) == 0;
    };
    if (ends_with(local, ".yaml")) {
      local.resize(local.size() - 5);
      local.append(".local.yaml");
    } else {
      local.append(".local.yaml");
    }
    struct stat st{};
    if (::stat(local.c_str(), &st) == 0 && S_ISREG(st.st_mode)) {
      std::string e2;
      if (!macros::load_append(local, &registry, &e2)) {
        trace::log("error", "failed to load macros overlay: " + e2);
      } else {
        trace::log("info", "loaded macros overlay: " + local);
      }
    }
  }
  seed_app_state_from_seqmem();
  start_app_observer();
  start_app_poller();
  actions::prewarm_app_cache();

  // Rich window context (title, URL, bundle_id) via AX queries.
  context::start_window_poller();
  // AFK detection via CGEventTap (timestamps only, never content).
  context::start_afk_monitor();

  // Optional: remote action-pack server (disabled by default).
  {
    Options ap = opts;
    bool loaded = maybe_load_action_pack_receiver_conf(&ap);
    if (loaded) {
      trace::log("info", "action-pack receiver config loaded");
    }
    action_pack_server::start_in_background(ap);
  }

  // Screen capture → local spool → Hetzner.
  // Best-effort: silently no-ops if Screen Recording isn't granted.
  {
    const char* home = std::getenv("HOME");
    std::string home_str;
    if (home && *home) {
      home_str = home;
    } else {
      struct passwd pwd{};
      struct passwd* result = nullptr;
      char buf_pw[16384];
      if (::getpwuid_r(::getuid(), &pwd, buf_pw, sizeof(buf_pw), &result) == 0 &&
          result && result->pw_dir && *result->pw_dir) {
        home_str = result->pw_dir;
      }
    }
    if (!home_str.empty()) {
      std::string app_support = home_str + "/Library/Application Support/seq";
      capture::Config cap_cfg;
      cap_cfg.spool_dir    = app_support + "/frames_spool";
      cap_cfg.fts_db_path  = app_support + "/seqmem_fts.db";
      cap_cfg.sync_script  = opts.root + "/tools/sync_frames.sh";
      cap_cfg.max_spool_mb = 200;
      capture::start(cap_cfg);
    }
  }

  return run_server(opts, registry);
}
}  // namespace seqd
