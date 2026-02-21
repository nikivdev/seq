#include "actions.h"
#include "action_pack_cli.h"
#include "io.h"
#include "options.h"
#include "process.h"
#include "seqd.h"
#include "strings.h"
#include "trace.h"

#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>

#include <errno.h>
#include <signal.h>
#include <sys/stat.h>
#include <algorithm>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <pwd.h>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/un.h>
#include <thread>
#include <vector>
#include <unistd.h>

using std::string_view;

namespace {
constexpr const char* kAppName = "seq";

uint64_t now_epoch_ms() {
  using namespace std::chrono;
  return (uint64_t)duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

std::string home_dir() {
  const char* home = ::getenv("HOME");
  if (home && *home) {
    return std::string(home);
  }
  // Hotkey helpers can run without HOME set. Fall back to passwd DB.
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
  if (home.empty()) {
    return {};
  }
  return std::filesystem::path(home) / ".config" / "seq";
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

constexpr const char* kCmdRun = "run";
constexpr const char* kCmdPing = "ping";
constexpr const char* kCmdHelp = "help";
constexpr const char* kCmdDaemon = "daemon";
constexpr const char* kCmdOpenApp = "open-app";
constexpr const char* kCmdOpenAppToggle = "open-app-toggle";
constexpr const char* kCmdAppState = "app-state";
constexpr const char* kCmdPerf = "perf";
constexpr const char* kCmdPerfSmoke = "perf-smoke";
constexpr const char* kCmdApps = "apps";
constexpr const char* kCmdMemMetrics = "mem-metrics";
constexpr const char* kCmdMemTail = "mem-tail";
constexpr const char* kCmdIncidentOpen = "incident-open";
constexpr const char* kCmdIncidentClose = "incident-close";
constexpr const char* kCmdAccessibilityPrompt = "accessibility-prompt";
constexpr const char* kCmdKeylog = "keylog";
constexpr const char* kCmdClick = "click";
constexpr const char* kCmdRightClick = "right-click";
constexpr const char* kCmdDoubleClick = "double-click";
constexpr const char* kCmdScroll = "scroll";
constexpr const char* kCmdDrag = "drag";
constexpr const char* kCmdMove = "move";
constexpr const char* kCmdScreenshot = "screenshot";
constexpr const char* kCmdAgent = "agent";
constexpr const char* kCmdActionPack = "action-pack";
constexpr const char* kCmdRpc = "rpc";

void print_usage(string_view name) {
  io::out.write(name);
  io::out.write(" - seq CLI\n");
  io::out.write("\nUSAGE:\n  ");
  io::out.write(name);
  io::out.write(" [options] <command> [args]\n\n");
  io::out.write("COMMANDS:\n");
  io::out.write("  run <macro>           Run a macro via seqd (fast path)\n");
  io::out.write("  open-app <name>       Open app without seqd\n");
  io::out.write("  open-app-toggle <name> Open app or Cmd-Tab if already frontmost\n");
  io::out.write("  app-state             Dump seqd cached frontmost/previous app\n");
  io::out.write("  perf                  Dump seqd perf stats (CPU time, RSS)\n");
  io::out.write("  perf-smoke [n] [ms]   Sample `perf` n times (default 20) every ms (default 100)\n");
  io::out.write("  apps                  List running apps (name/bundle_id/pid/bundle_url)\n");
  io::out.write("  mem-metrics           Query seq memory engine metrics\n");
  io::out.write("  mem-tail <n>          Tail last N memory engine events\n");
  io::out.write("  incident-open <id> <title>  Record incident start marker\n");
  io::out.write("  incident-close <id> [resolution] Record incident end marker\n");
  io::out.write("  accessibility-prompt  Trigger Accessibility permission prompt\n");
  io::out.write("  keylog                Log key events for debugging (default 10s)\n");
  io::out.write("  click <x> <y>         Left click at coordinates\n");
  io::out.write("  right-click <x> <y>   Right click at coordinates\n");
  io::out.write("  double-click <x> <y>  Double click at coordinates\n");
  io::out.write("  scroll <x> <y> <dy>   Scroll at coordinates (dy: lines, negative=up)\n");
  io::out.write("  drag <x1> <y1> <x2> <y2>  Drag from (x1,y1) to (x2,y2)\n");
  io::out.write("  move <x> <y>          Move mouse to coordinates\n");
  io::out.write("  screenshot [path]     Capture screen (default: /tmp/seq_screenshot.png)\n");
  io::out.write("  agent <instruction>   Run UI-TARS computer use agent\n");
  io::out.write("  rpc <json>            Send typed JSON RPC request to seqd\n");
  io::out.write("  action-pack ...       Signed remote action packs (see: seq action-pack help)\n");
  io::out.write("  ping                  Ping seqd\n");
  io::out.write("  help                  Show this help\n");
  io::out.write("\nOPTIONS (global; must appear before <command>):\n");
  io::out.write("  --socket <path>       Override socket path (default: /tmp/seqd.sock)\n");
  io::out.write("  --mem-socket <path>   Override legacy seqmemd query socket (default: /tmp/seqmemq.sock)\n");
  io::out.write("  --root <path>         Seq root (default: /Users/nikiv/code/seq)\n");
  io::out.write("  --macros <path>       Macros file (default: /Users/nikiv/code/seq/seq.macros.yaml)\n");
  io::out.write("  --seconds <n>         Duration for keylog (default: 10)\n");
  io::out.write("  --action-pack-listen <ip:port>  (daemon only) enable action-pack TCP server\n");
  io::out.write("  --action-pack-pubkeys <path>    (daemon only) key_id<ws>base64(pubkey)\n");
  io::out.write("  --action-pack-policy <path>     (daemon only) optional policy file\n");
  io::out.write("  --action-pack-root <path>       (daemon only) restrict cwd/relative cmds under root\n");
  io::out.write("  --action-pack-max-conns <n>     (daemon only) connection concurrency limit\n");
  io::out.write("  --action-pack-io-timeout-ms <n> (daemon only) socket read/write timeout\n");
}

int connect_socket(const std::string& path) {
  int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd < 0) {
    return -1;
  }
  sockaddr_un addr{};
  addr.sun_family = AF_UNIX;
  if (path.size() >= sizeof(addr.sun_path)) {
    ::close(fd);
    errno = ENAMETOOLONG;
    return -1;
  }
  std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path.c_str());
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
  if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), addrlen) != 0) {
    ::close(fd);
    return -1;
  }
  return fd;
}

bool write_all(int fd, const void* data, size_t len) {
  const char* ptr = static_cast<const char*>(data);
  size_t offset = 0;
  while (offset < len) {
    ssize_t n = ::write(fd, ptr + offset, len - offset);
    if (n > 0) {
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

bool read_all(int fd, std::string* out) {
  char buf[512];
  while (true) {
    ssize_t n = ::read(fd, buf, sizeof(buf));
    if (n > 0) {
      for (ssize_t i = 0; i < n; ++i) {
        out->push_back(buf[i]);
        if (buf[i] == '\n') {
          return true;
        }
      }
      // Guard against unbounded responses if peer never sends '\n'.
      if (out->size() > 1u * 1024u * 1024u) {
        errno = EMSGSIZE;
        return false;
      }
      continue;
    }
    if (n == 0) {
      return !out->empty();
    }
    if (n < 0 && errno == EINTR) {
      continue;
    }
    return false;
  }
}

std::string join_args(int argc, char** argv, int start) {
  if (start >= argc) {
    return std::string();
  }
  std::string out;
  for (int i = start; i < argc; ++i) {
    if (i != start) {
      out.push_back(' ');
    }
    out.append(argv[i]);
  }
  return out;
}

bool try_send_request(const Options& opts, string_view payload, std::string* response) {
  int fd = connect_socket(opts.socket_path);
  if (fd < 0) {
    return false;
  }
  bool ok = write_all(fd, payload.data(), payload.size());
  if (!ok) {
    ::close(fd);
    return false;
  }
  ::shutdown(fd, SHUT_WR);
  ok = read_all(fd, response);
  ::close(fd);
  if (!ok) {
    return false;
  }
  return true;
}

int send_request(const Options& opts, string_view payload, std::string* response) {
  if (!try_send_request(opts, payload, response)) {
    io::err.write("error: unable to connect to seqd at ");
    io::err.write(opts.socket_path);
    io::err.write("\n");
    trace::log("error", "connect failed");
    return 1;
  }
  return 0;
}

int cmd_ping(const Options& opts) {
  std::string response;
  int rc = send_request(opts, "PING\n", &response);
  if (rc != 0) {
    return rc;
  }
  io::out.write(response);
  if (!response.empty() && response.back() != '\n') {
    io::out.write("\n");
  }
  return 0;
}

int cmd_rpc(const Options& opts, const std::string& payload) {
  if (payload.empty()) {
    io::err.write("error: rpc requires a JSON payload\n");
    return 1;
  }
  std::string request = payload;
  if (request.back() != '\n') {
    request.push_back('\n');
  }
  std::string response;
  int rc = send_request(opts, request, &response);
  if (rc != 0) {
    return rc;
  }
  io::out.write(response);
  if (!response.empty() && response.back() != '\n') {
    io::out.write("\n");
  }
  if (response.find("\"ok\":false") != std::string::npos) {
    return 1;
  }
  return 0;
}

int cmd_app_state(const Options& opts) {
  std::string response;
  int rc = send_request(opts, "APP_STATE\n", &response);
  if (rc != 0) {
    return rc;
  }
  io::out.write(response);
  if (!response.empty() && response.back() != '\n') {
    io::out.write("\n");
  }
  return 0;
}

int cmd_perf(const Options& opts) {
  std::string response;
  int rc = send_request(opts, "PERF\n", &response);
  if (rc != 0) {
    return rc;
  }
  io::out.write(response);
  if (!response.empty() && response.back() != '\n') {
    io::out.write("\n");
  }
  return 0;
}

bool json_u64_field(std::string_view json, std::string_view key, std::uint64_t* out) {
  if (!out) {
    return false;
  }
  std::string needle;
  needle.reserve(key.size() + 4);
  needle.push_back('"');
  needle.append(key.data(), key.size());
  needle.append("\":");
  auto pos = json.find(needle);
  if (pos == std::string_view::npos) {
    return false;
  }
  pos += needle.size();
  while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t')) {
    ++pos;
  }
  const char* begin = json.data() + pos;
  const char* end = json.data() + json.size();
  std::uint64_t value = 0;
  auto parsed = std::from_chars(begin, end, value);
  if (parsed.ec != std::errc{}) {
    return false;
  }
  *out = value;
  return true;
}

int cmd_perf_smoke(const Options& opts, int argc, char** argv, int index) {
  int samples = 20;
  int sleep_ms = 100;
  if (index < argc) {
    samples = std::atoi(argv[index]);
    ++index;
  }
  if (index < argc) {
    sleep_ms = std::atoi(argv[index]);
    ++index;
  }
  if (samples < 2) {
    io::err.write("error: perf-smoke requires at least 2 samples\n");
    return 1;
  }
  if (sleep_ms < 0) {
    io::err.write("error: perf-smoke sleep ms must be >= 0\n");
    return 1;
  }

  struct PerfSample {
    std::uint64_t push_calls = 0;
    std::uint64_t wake_count = 0;
    std::uint64_t flush_count = 0;
    std::uint64_t total_flush_us = 0;
    std::uint64_t max_flush_us = 0;
    std::uint64_t last_flush_us = 0;
    std::uint64_t last_pending_rows = 0;
    std::uint64_t max_pending_rows = 0;
    std::uint64_t inserted_count = 0;
    std::uint64_t error_count = 0;
  };

  auto read_sample = [&](PerfSample* out) -> bool {
    if (!out) {
      return false;
    }
    std::string response;
    if (send_request(opts, "PERF\n", &response) != 0) {
      return false;
    }
    json_u64_field(response, "push_calls", &out->push_calls);
    json_u64_field(response, "wake_count", &out->wake_count);
    json_u64_field(response, "flush_count", &out->flush_count);
    json_u64_field(response, "total_flush_us", &out->total_flush_us);
    json_u64_field(response, "max_flush_us", &out->max_flush_us);
    json_u64_field(response, "last_flush_us", &out->last_flush_us);
    json_u64_field(response, "last_pending_rows", &out->last_pending_rows);
    json_u64_field(response, "max_pending_rows", &out->max_pending_rows);
    json_u64_field(response, "inserted_count", &out->inserted_count);
    json_u64_field(response, "error_count", &out->error_count);
    return true;
  };

  PerfSample first{};
  PerfSample last{};
  if (!read_sample(&first)) {
    return 1;
  }
  last = first;
  for (int i = 1; i < samples; ++i) {
    if (sleep_ms > 0) {
      std::this_thread::sleep_for(std::chrono::milliseconds(sleep_ms));
    }
    if (!read_sample(&last)) {
      return 1;
    }
  }

  auto delta = [](std::uint64_t before, std::uint64_t after) -> std::uint64_t {
    return after >= before ? after - before : 0;
  };

  const std::uint64_t d_push = delta(first.push_calls, last.push_calls);
  const std::uint64_t d_wake = delta(first.wake_count, last.wake_count);
  const std::uint64_t d_flush = delta(first.flush_count, last.flush_count);
  const std::uint64_t d_total_flush_us = delta(first.total_flush_us, last.total_flush_us);
  const std::uint64_t d_inserted = delta(first.inserted_count, last.inserted_count);
  const std::uint64_t d_errors = delta(first.error_count, last.error_count);
  const std::uint64_t avg_flush_us = d_flush ? (d_total_flush_us / d_flush) : 0;

  std::string out;
  out.reserve(512);
  out.append("{\"samples\":").append(std::to_string(samples));
  out.append(",\"sleep_ms\":").append(std::to_string(sleep_ms));
  out.append(",\"delta\":{");
  out.append("\"push_calls\":").append(std::to_string(d_push));
  out.append(",\"wake_count\":").append(std::to_string(d_wake));
  out.append(",\"flush_count\":").append(std::to_string(d_flush));
  out.append(",\"total_flush_us\":").append(std::to_string(d_total_flush_us));
  out.append(",\"avg_flush_us\":").append(std::to_string(avg_flush_us));
  out.append(",\"inserted_count\":").append(std::to_string(d_inserted));
  out.append(",\"error_count\":").append(std::to_string(d_errors));
  out.append("},\"last\":{");
  out.append("\"max_flush_us\":").append(std::to_string(last.max_flush_us));
  out.append(",\"last_flush_us\":").append(std::to_string(last.last_flush_us));
  out.append(",\"last_pending_rows\":").append(std::to_string(last.last_pending_rows));
  out.append(",\"max_pending_rows\":").append(std::to_string(last.max_pending_rows));
  out.append("}}");
  io::out.write(out);
  io::out.write("\n");
  return 0;
}

int cmd_mem_metrics(const Options& opts) {
  std::string response;
  int rc = send_request(opts, "MEM_METRICS\n", &response);
  if (rc != 0) {
    return rc;
  }
  io::out.write(response);
  if (!response.empty() && response.back() != '\n') {
    io::out.write("\n");
  }
  return 0;
}

int cmd_mem_tail(const Options& opts, string_view n_str) {
  std::string response;
  std::string request = "MEM_TAIL ";
  request.append(n_str.data(), n_str.size());
  request.push_back('\n');
  int rc = send_request(opts, request, &response);
  if (rc != 0) {
    return rc;
  }
  io::out.write(response);
  if (!response.empty() && response.back() != '\n') {
    io::out.write("\n");
  }
  return 0;
}

int cmd_incident_open(const Options& opts, string_view id, string_view title) {
  std::string request;
  request.reserve(id.size() + title.size() + 16);
  request.append("INCIDENT_OPEN ");
  request.append(id.data(), id.size());
  request.push_back(' ');
  request.append(title.data(), title.size());
  request.push_back('\n');
  std::string response;
  int rc = send_request(opts, request, &response);
  if (rc != 0) {
    return rc;
  }
  if (response.rfind("ERR", 0) == 0) {
    io::err.write(response);
    if (!response.empty() && response.back() != '\n') {
      io::err.write("\n");
    }
    return 1;
  }
  io::out.write(response);
  if (!response.empty() && response.back() != '\n') {
    io::out.write("\n");
  }
  return 0;
}

int cmd_incident_close(const Options& opts, string_view id, string_view resolution) {
  std::string request;
  request.reserve(id.size() + resolution.size() + 20);
  request.append("INCIDENT_CLOSE ");
  request.append(id.data(), id.size());
  if (!resolution.empty()) {
    request.push_back(' ');
    request.append(resolution.data(), resolution.size());
  }
  request.push_back('\n');
  std::string response;
  int rc = send_request(opts, request, &response);
  if (rc != 0) {
    return rc;
  }
  if (response.rfind("ERR", 0) == 0) {
    io::err.write(response);
    if (!response.empty() && response.back() != '\n') {
      io::err.write("\n");
    }
    return 1;
  }
  io::out.write(response);
  if (!response.empty() && response.back() != '\n') {
    io::out.write("\n");
  }
  return 0;
}

int cmd_run(const Options& opts, string_view macro) {
  trace::event("cli.run", macro);

  // Local-first execution for UI macros.
  //
  // Many macros are about app activation + keystrokes. Running those from seqd
  // (a background daemon) is less reliable on newer macOS due to focus/activation
  // restrictions. The client process launched by the hotkey is treated as
  // "user initiated" more often, so do best-effort local execution first.
  macros::Registry registry;
  std::string load_error;
  auto load_with_overlay = [&](macros::Registry* out) -> bool {
    if (!macros::load(opts.macros, out, &load_error)) {
      return false;
    }
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
      (void)macros::load_append(local, out, &e2);
    }
    // Also load user-scoped macros grouped by app:
    //   ~/.config/seq/apps/<app>/*.yaml
    // This is intentionally best-effort (errors don't block base macros).
    append_seq_user_app_macros(out);
    return true;
  };

  if (load_with_overlay(&registry)) {
    const macros::Macro* m = macros::find(registry, macro);
    if (m && m->action != macros::ActionType::Todo && m->action != macros::ActionType::Unknown) {
      auto t0 = std::chrono::steady_clock::now();
      trace::event("cli.run.local", macro);
      actions::Result r = actions::run(*m);
      auto t1 = std::chrono::steady_clock::now();
      uint64_t dur_us = (uint64_t)std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count();

      // Best-effort: also record to seqd mem log for observability (ignored if seqd down).
      {
        std::string req;
        req.reserve(macro.size() + 96);
        req.append("TRACE cli.run.local\t");
        req.append(std::to_string(now_epoch_ms()));
        req.push_back('\t');
        req.append(std::to_string(dur_us));
        req.push_back('\t');
        req.append(r.ok ? "1" : "0");
        req.push_back('\t');
        req.append(std::string(macro));
        req.push_back('\n');
        std::string ignored;
        (void)try_send_request(opts, req, &ignored);
      }

      if (!r.ok) {
        io::err.write("ERR ");
        io::err.write(r.error);
        io::err.write("\n");
        return 1;
      }
      io::out.write("OK\n");
      return 0;
    }
  }

  std::string request;
  request.reserve(macro.size() + 5);
  request.append("RUN ");
  request.append(macro.data(), macro.size());
  request.push_back('\n');

  std::string response;
  int rc = send_request(opts, request, &response);
  if (rc != 0) {
    return rc;
  }
  if (response.rfind("ERR", 0) == 0) {
    io::err.write(response);
    if (!response.empty() && response.back() != '\n') {
      io::err.write("\n");
    }
    return 1;
  }
  io::out.write(response);
  if (!response.empty() && response.back() != '\n') {
    io::out.write("\n");
  }
  return 0;
}

int cmd_open_app_toggle(const Options& opts, string_view app) {
  trace::event("cli.open_app_toggle", app);
  std::string target(app);
  actions::FrontmostApp front_info = actions::frontmost_app();
  std::string front = front_info.name;
  std::string prev;
  std::string decision;

  actions::Result result{true, ""};
  auto is_target_front = [&]() -> bool {
    if (target.empty()) return false;
    if (!front_info.name.empty() && front_info.name == target) return true;
    if (!front_info.bundle_id.empty() && front_info.bundle_id == target) return true;
    // If target looks like an app bundle path, allow matching by bundle_url.
    auto ends_with_app = [&]() -> bool {
      return target.size() >= 4 && target.compare(target.size() - 4, 4, ".app") == 0;
    };
    if ((target.find('/') != std::string::npos || ends_with_app()) &&
        !front_info.bundle_url.empty() && front_info.bundle_url == target) {
      return true;
    }
    return false;
  };

  if (is_target_front()) {
    // Keyboard Maestro behavior: if already in the app, activate the last app.
    //
    // Prefer direct activation of the last app tracked by seqd (fast, no UI).
    // Only fall back to Cmd-Tab if we can't determine the previous app, or if
    // activation fails.
    std::string response;
    if (try_send_request(opts, "PREV_APP\n", &response)) {
      prev = strings::trim(response);
    }
    if (!prev.empty() && prev != front) {
      decision = "open_prev";
      result = actions::open_app(prev);
      if (!result.ok) {
        // Rare: if activation fails, try Cmd-Tab as last resort (only if we can).
        if (AXIsProcessTrusted()) {
          decision = "cmd_tab_fallback";
          result = actions::open_app_toggle(target);
        }
      }
    } else if (AXIsProcessTrusted()) {
      decision = "cmd_tab_fallback";
      result = actions::open_app_toggle(target);
    } else {
      decision = "no_prev";
      result = {true, ""};
    }
  } else {
    decision = "open_target";
    result = actions::open_app(target);
  }

  // Always log locally so hotkey-driven runs are debuggable even if seqd is down.
  {
    std::string subj;
    subj.reserve(target.size() + front.size() + prev.size() + decision.size() + 64);
    subj.append("target=").append(target);
    subj.append("\tfront=").append(front);
    subj.append("\tprev=").append(prev);
    subj.append("\tdecision=").append(decision);
    trace::event("cli.open_app_toggle.action", subj);
  }

  // Best-effort: log a structured breadcrumb to seqd for debugging.
  {
    std::string subj;
    subj.reserve(target.size() + front.size() + prev.size() + decision.size() + 64);
    subj.append("target=").append(target);
    subj.append("\tfront=").append(front);
    subj.append("\tprev=").append(prev);
    subj.append("\tdecision=").append(decision);
    std::string req;
    req.reserve(subj.size() + 64);
    req.append("TRACE cli.open_app_toggle.action\t");
    req.append(std::to_string(now_epoch_ms()));
    req.append("\t0\t");
    req.append(result.ok ? "1" : "0");
    req.append("\t");
    req.append(subj);
    req.push_back('\n');
    std::string ignored;
    (void)try_send_request(opts, req, &ignored);
  }

  if (!result.ok) {
    io::err.write("ERR ");
    io::err.write(result.error);
    io::err.write("\n");
    return 1;
  }
  io::out.write("OK\n");
  return 0;
}

int cmd_accessibility_prompt(const Options& opts) {
  const void* keys[] = {kAXTrustedCheckOptionPrompt};
  const void* values[] = {kCFBooleanTrue};
  CFDictionaryRef options = CFDictionaryCreate(
      kCFAllocatorDefault,
      keys,
      values,
      1,
      &kCFCopyStringDictionaryKeyCallBacks,
      &kCFTypeDictionaryValueCallBacks);
  bool trusted = AXIsProcessTrustedWithOptions(options);
  if (options) {
    CFRelease(options);
  }
  trace::event("cli.accessibility_prompt.local", trusted ? "trusted" : "not_trusted");

  // Also prompt seqd so socket-driven macros (Karabiner `seqSocket(...)`) can
  // do menu/keystroke automation without falling back to local `seq run`.
  bool daemon_trusted = true;
  bool daemon_reachable = false;
  std::string daemon_exe;
  std::string daemon_resp;
  (void)try_send_request(opts, "AX_EXE\n", &daemon_exe);
  daemon_exe = strings::trim(daemon_exe);
  if (try_send_request(opts, "AX_PROMPT\n", &daemon_resp)) {
    daemon_reachable = true;
    daemon_trusted = (daemon_resp.rfind("OK", 0) == 0);
    trace::event("cli.accessibility_prompt.seqd", daemon_trusted ? "trusted" : "not_trusted");
  } else {
    trace::event("cli.accessibility_prompt.seqd", "unreachable");
  }

  if (trusted && daemon_trusted) {
    io::out.write("OK\n");
    return 0;
  }
  io::err.write("ERR accessibility not trusted (local=");
  io::err.write(trusted ? "1" : "0");
  io::err.write(" seqd=");
  if (!daemon_reachable) {
    io::err.write("unreachable");
  } else {
    io::err.write(daemon_trusted ? "1" : "0");
  }
  if (!daemon_exe.empty()) {
    io::err.write(" seqd_exe=");
    io::err.write(daemon_exe);
  }
  io::err.write(")\n");
  return 1;
}

struct KeylogState {
  CFMachPortRef tap = nullptr;
};

CGEventRef keylog_callback(CGEventTapProxy proxy,
                           CGEventType type,
                           CGEventRef event,
                           void* refcon) {
  (void)proxy;
  KeylogState* state = static_cast<KeylogState*>(refcon);
  if (type == kCGEventTapDisabledByTimeout && state && state->tap) {
    CGEventTapEnable(state->tap, true);
    return event;
  }
  if (type == kCGEventKeyDown || type == kCGEventKeyUp) {
    int64_t keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode);
    CGEventFlags flags = CGEventGetFlags(event);
    char buf[96];
    std::snprintf(buf,
                  sizeof(buf),
                  "type=%s keycode=%lld flags=0x%llx",
                  type == kCGEventKeyDown ? "down" : "up",
                  static_cast<long long>(keycode),
                  static_cast<unsigned long long>(flags));
    trace::event("keylog", buf);
  } else if (type == kCGEventFlagsChanged) {
    CGEventFlags flags = CGEventGetFlags(event);
    char buf[64];
    std::snprintf(buf, sizeof(buf), "type=flags flags=0x%llx",
                  static_cast<unsigned long long>(flags));
    trace::event("keylog", buf);
  }
  return event;
}

int cmd_keylog(int argc, char** argv, int index) {
  int seconds = 10;
  while (index < argc) {
    std::string_view arg = argv[index];
    if (arg == "--seconds") {
      if (index + 1 >= argc) {
        io::err.write("error: --seconds requires a value\n");
        return 1;
      }
      seconds = std::max(1, std::atoi(argv[index + 1]));
      index += 2;
      continue;
    }
    io::err.write("error: unknown keylog option\n");
    return 1;
  }

  trace::event("cli.keylog.start", std::to_string(seconds));

  KeylogState state;
  CGEventMask mask = CGEventMaskBit(kCGEventKeyDown) |
                     CGEventMaskBit(kCGEventKeyUp) |
                     CGEventMaskBit(kCGEventFlagsChanged);
  state.tap = CGEventTapCreate(kCGHIDEventTap,
                               kCGTailAppendEventTap,
                               kCGEventTapOptionListenOnly,
                               mask,
                               keylog_callback,
                               &state);
  if (!state.tap) {
    io::err.write("error: keylog event tap failed (check Input Monitoring)\n");
    trace::event("cli.keylog.error", "tap_create_failed");
    return 1;
  }

  CFRunLoopSourceRef source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, state.tap, 0);
  CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes);
  CFRelease(source);
  CGEventTapEnable(state.tap, true);

  auto end_time = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
  while (std::chrono::steady_clock::now() < end_time) {
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, true);
  }

  CFRelease(state.tap);
  state.tap = nullptr;

  trace::event("cli.keylog.stop", "done");
  io::out.write("OK\n");
  return 0;
}
}  // namespace

int main(int argc, char** argv) {
  signal(SIGPIPE, SIG_IGN);
  string_view name = argc > 0 ? argv[0] : kAppName;
  trace::Guard guard(kAppName);

  if (argc < 2) {
    print_usage(name);
    return 1;
  }

  Options opts = default_options();
  int index = 1;
  if (!parse_options(argc, argv, &index, &opts)) {
    return 1;
  }
  if (index >= argc) {
    print_usage(name);
    return 1;
  }

  string_view cmd = argv[index++];
  // Allow global flags both before and after the subcommand. (Flow daemon configs
  // pass flags after the subcommand.)
  if (!parse_options(argc, argv, &index, &opts)) {
    return 1;
  }
  if (cmd == kCmdDaemon) {
    return seqd::run_daemon(opts);
  }
  if (cmd == kCmdActionPack) {
    return cmd_action_pack(argc, argv, index, opts);
  }
  if (cmd == kCmdRpc) {
    if (index >= argc) {
      io::err.write("error: rpc requires JSON payload\n");
      return 1;
    }
    std::string payload = join_args(argc, argv, index);
    return cmd_rpc(opts, payload);
  }
  if (cmd == kCmdHelp) {
    print_usage(name);
    return 0;
  }
  if (cmd == kCmdPing) {
    return cmd_ping(opts);
  }
  if (cmd == kCmdAppState) {
    return cmd_app_state(opts);
  }
  if (cmd == kCmdPerf) {
    return cmd_perf(opts);
  }
  if (cmd == kCmdPerfSmoke) {
    return cmd_perf_smoke(opts, argc, argv, index);
  }
  if (cmd == kCmdApps) {
    trace::event("cli.apps", "list");
    std::string json = actions::running_apps_json();
    io::out.write(json);
    io::out.write("\n");
    return 0;
  }
  if (cmd == kCmdAccessibilityPrompt) {
    return cmd_accessibility_prompt(opts);
  }
  if (cmd == kCmdKeylog) {
    return cmd_keylog(argc, argv, index);
  }
  if (cmd == kCmdOpenApp || cmd == kCmdOpenAppToggle) {
    if (index >= argc) {
      io::err.write("error: open-app requires an app name\n");
      return 1;
    }
    string_view app = argv[index];
    if (cmd == kCmdOpenApp) {
      trace::event("cli.open_app", app);
      actions::Result result = actions::open_app(app);
      if (!result.ok) {
        io::err.write("ERR ");
        io::err.write(result.error);
        io::err.write("\n");
        return 1;
      }
      io::out.write("OK\n");
      return 0;
    }
    return cmd_open_app_toggle(opts, app);
  }
  if (cmd == kCmdRun) {
    if (index >= argc) {
      io::err.write("error: run requires a macro name\n");
      return 1;
    }
    string_view macro = argv[index];
    return cmd_run(opts, macro);
  }
  if (cmd == kCmdMemMetrics) {
    return cmd_mem_metrics(opts);
  }
  if (cmd == kCmdMemTail) {
    if (index >= argc) {
      io::err.write("error: mem-tail requires a number\n");
      return 1;
    }
    return cmd_mem_tail(opts, argv[index]);
  }
  if (cmd == kCmdIncidentOpen) {
    if (index + 1 >= argc) {
      io::err.write("error: incident-open requires <id> <title>\n");
      return 1;
    }
    string_view id = argv[index];
    std::string title = join_args(argc, argv, index + 1);
    return cmd_incident_open(opts, id, title);
  }
  if (cmd == kCmdIncidentClose) {
    if (index >= argc) {
      io::err.write("error: incident-close requires <id>\n");
      return 1;
    }
    string_view id = argv[index];
    std::string resolution = join_args(argc, argv, index + 1);
    return cmd_incident_close(opts, id, resolution);
  }

  if (cmd == kCmdClick || cmd == kCmdDoubleClick || cmd == kCmdRightClick || cmd == kCmdMove) {
    if (index + 1 >= argc) {
      io::err.write("error: ");
      io::err.write(cmd);
      io::err.write(" requires <x> <y>\n");
      return 1;
    }
    double x = std::strtod(argv[index], nullptr);
    double y = std::strtod(argv[index + 1], nullptr);
    std::string label = "cli.";
    label.append(cmd);
    trace::event(label, std::string(argv[index]) + " " + argv[index + 1]);
    actions::Result r{false, ""};
    if (cmd == kCmdClick) r = actions::mouse_click(x, y);
    else if (cmd == kCmdDoubleClick) r = actions::mouse_double_click(x, y);
    else if (cmd == kCmdRightClick) r = actions::mouse_right_click(x, y);
    else if (cmd == kCmdMove) r = actions::mouse_move(x, y);
    if (!r.ok) {
      io::err.write("ERR ");
      io::err.write(r.error);
      io::err.write("\n");
      return 1;
    }
    // Best-effort trace to seqd for observability.
    {
      std::string req;
      req.append("TRACE ");
      req.append(label);
      req.push_back('\t');
      req.append(std::to_string(now_epoch_ms()));
      req.append("\t0\t1\t");
      req.append(argv[index]);
      req.push_back(' ');
      req.append(argv[index + 1]);
      req.push_back('\n');
      std::string ignored;
      (void)try_send_request(opts, req, &ignored);
    }
    io::out.write("OK\n");
    return 0;
  }
  if (cmd == kCmdScroll) {
    if (index + 2 >= argc) {
      io::err.write("error: scroll requires <x> <y> <dy>\n");
      return 1;
    }
    double x = std::strtod(argv[index], nullptr);
    double y = std::strtod(argv[index + 1], nullptr);
    int dy = std::atoi(argv[index + 2]);
    trace::event("cli.scroll", std::string(argv[index]) + " " + argv[index + 1] + " " + argv[index + 2]);
    actions::Result r = actions::mouse_scroll(x, y, dy);
    if (!r.ok) {
      io::err.write("ERR ");
      io::err.write(r.error);
      io::err.write("\n");
      return 1;
    }
    {
      std::string req;
      req.append("TRACE cli.scroll\t");
      req.append(std::to_string(now_epoch_ms()));
      req.append("\t0\t1\t");
      req.append(argv[index]);
      req.push_back(' ');
      req.append(argv[index + 1]);
      req.push_back(' ');
      req.append(argv[index + 2]);
      req.push_back('\n');
      std::string ignored;
      (void)try_send_request(opts, req, &ignored);
    }
    io::out.write("OK\n");
    return 0;
  }
  if (cmd == kCmdDrag) {
    if (index + 3 >= argc) {
      io::err.write("error: drag requires <x1> <y1> <x2> <y2>\n");
      return 1;
    }
    double x1 = std::strtod(argv[index], nullptr);
    double y1 = std::strtod(argv[index + 1], nullptr);
    double x2 = std::strtod(argv[index + 2], nullptr);
    double y2 = std::strtod(argv[index + 3], nullptr);
    trace::event("cli.drag", std::string(argv[index]) + " " + argv[index + 1] + " " + argv[index + 2] + " " + argv[index + 3]);
    actions::Result r = actions::mouse_drag(x1, y1, x2, y2);
    if (!r.ok) {
      io::err.write("ERR ");
      io::err.write(r.error);
      io::err.write("\n");
      return 1;
    }
    {
      std::string req;
      req.append("TRACE cli.drag\t");
      req.append(std::to_string(now_epoch_ms()));
      req.append("\t0\t1\t");
      req.append(argv[index]);
      req.push_back(' ');
      req.append(argv[index + 1]);
      req.push_back(' ');
      req.append(argv[index + 2]);
      req.push_back(' ');
      req.append(argv[index + 3]);
      req.push_back('\n');
      std::string ignored;
      (void)try_send_request(opts, req, &ignored);
    }
    io::out.write("OK\n");
    return 0;
  }
  if (cmd == kCmdScreenshot) {
    std::string path = (index < argc) ? argv[index] : "/tmp/seq_screenshot.png";
    trace::event("cli.screenshot", path);
    actions::Result r = actions::screenshot(path);
    if (!r.ok) {
      io::err.write("ERR ");
      io::err.write(r.error);
      io::err.write("\n");
      return 1;
    }
    {
      std::string req;
      req.append("TRACE cli.screenshot\t");
      req.append(std::to_string(now_epoch_ms()));
      req.append("\t0\t1\t");
      req.append(path);
      req.push_back('\n');
      std::string ignored;
      (void)try_send_request(opts, req, &ignored);
    }
    io::out.write(path);
    io::out.write("\n");
    return 0;
  }

  if (cmd == kCmdAgent) {
    if (index >= argc) {
      io::err.write("error: agent requires an instruction\n");
      return 1;
    }
    std::string instruction = join_args(argc, argv, index);
    trace::event("cli.agent", instruction);
    std::string agent_py = opts.root + "/agent.py";
    std::vector<std::string> args = {"/usr/bin/python3", agent_py, instruction};
    std::string error;
    // Use blocking run so terminal usage shows output. For hotkey usage,
    // the caller (Karabiner shell) should background with &.
    int code = process::run(args, &error);
    {
      std::string req;
      req.append("TRACE cli.agent\t");
      req.append(std::to_string(now_epoch_ms()));
      req.append("\t0\t");
      req.append(code == 0 ? "1" : "0");
      req.append("\t");
      req.append(instruction);
      req.push_back('\n');
      std::string ignored;
      (void)try_send_request(opts, req, &ignored);
    }
    if (code != 0) {
      io::err.write("ERR agent exited with code ");
      io::err.write(std::to_string(code));
      io::err.write("\n");
      return 1;
    }
    return 0;
  }

  io::err.write("error: unknown command\n");
  print_usage(name);
  return 1;
}
