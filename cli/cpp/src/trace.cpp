#include "trace.h"

#include <algorithm>
#include <cerrno>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <mutex>
#include <string>

#include <fcntl.h>
#include <pthread.h>
#include <pwd.h>
#include <unistd.h>

#ifdef SEQ_HAS_CLICKHOUSE
#include "clickhouse.h"
#endif

namespace trace {
namespace {
int cli_fd = -1;
int trace_fd = -1;
bool initialized = false;
std::string app_name;

#ifdef SEQ_HAS_CLICKHOUSE
// Native ClickHouse writer.
std::unique_ptr<seq::ch::AsyncWriter> ch_writer;
enum class ChMode {
  kNative,
  kMirror,
  kFile,
  kOff,
};
ChMode ch_mode = ChMode::kNative;
#endif

int ch_fd = -1;
std::mutex ch_mu;

#ifdef SEQ_HAS_CLICKHOUSE
std::string to_lower_ascii(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
    return static_cast<char>(std::tolower(c));
  });
  return value;
}

const char* ch_mode_name(ChMode mode) {
  switch (mode) {
    case ChMode::kNative:
      return "native";
    case ChMode::kMirror:
      return "mirror";
    case ChMode::kFile:
      return "file";
    case ChMode::kOff:
      return "off";
  }
  return "file";
}
#endif

#ifdef SEQ_HAS_CLICKHOUSE
ChMode parse_ch_mode() {
  const char* raw = std::getenv("SEQ_CH_MODE");
  if (!raw || !*raw) {
    // Default to local spool to keep user-path latency independent of network state.
    return ChMode::kFile;
  }
  const std::string mode = to_lower_ascii(raw);
  if (mode == "mirror" || mode == "dual") {
    return ChMode::kMirror;
  }
  if (mode == "file" || mode == "spool" || mode == "local-file") {
    return ChMode::kFile;
  }
  if (mode == "off" || mode == "none" || mode == "disabled") {
    return ChMode::kOff;
  }
  // Keep these aliases to avoid surprises while migrating.
  if (mode == "native" || mode == "local" || mode == "remote" || mode == "remote-only") {
    return ChMode::kNative;
  }
  return ChMode::kFile;
}
#endif

int64_t now_us() {
  using namespace std::chrono;
  return duration_cast<microseconds>(system_clock::now().time_since_epoch()).count();
}

void write_all(int fd, const char* data, size_t len) {
  size_t offset = 0;
  while (offset < len) {
    ssize_t n = ::write(fd, data + offset, len - offset);
    if (n > 0) {
      offset += static_cast<size_t>(n);
      continue;
    }
    if (n < 0 && errno == EINTR) {
      continue;
    }
    break;
  }
}

void write_line(int fd, std::string_view level, std::string_view msg) {
  if (fd < 0) {
    return;
  }
  char header[96];
  int n = std::snprintf(
      header,
      sizeof(header),
      "%lld [%.*s] ",
      static_cast<long long>(now_us()),
      static_cast<int>(level.size()),
      level.data());
  if (n > 0) {
    write_all(fd, header, static_cast<size_t>(n));
  }
  if (!msg.empty()) {
    write_all(fd, msg.data(), msg.size());
  }
  write_all(fd, "\n", 1);
}

std::string resolve_log_dir() {
  const char* env = std::getenv("RISE_LOG_DIR");
  if (env && *env) {
    return std::string(env);
  }
  return "out/logs";
}

int open_log(const std::string& path) {
  return ::open(path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
}

uint64_t thread_id() {
  uint64_t tid = 0;
  pthread_threadid_np(nullptr, &tid);
  return tid;
}

void append_number(std::string& out, long long value) {
  char buf[32];
  int n = std::snprintf(buf, sizeof(buf), "%lld", value);
  if (n > 0) {
    out.append(buf, static_cast<size_t>(n));
  }
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

std::string resolve_ch_log_path() {
  const char* env = std::getenv("SEQ_CH_LOG_PATH");
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
  path += "/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl";
  return path;
}

void write_ch_row(std::string_view level,
                  std::string_view kind,
                  std::string_view name,
                  std::string_view message,
                  long long dur_us) {
  if (ch_fd < 0) {
    return;
  }
  std::string line;
  line.reserve(256 + name.size() + message.size());
  line.push_back('{');
  line.append("\"ts_us\":");
  append_number(line, now_us());
  line.append(",\"app\":");
  append_json_string(line, app_name);
  line.append(",\"pid\":");
  append_number(line, static_cast<long long>(::getpid()));
  line.append(",\"tid\":");
  append_number(line, static_cast<long long>(thread_id()));
  line.append(",\"level\":");
  append_json_string(line, level);
  line.append(",\"kind\":");
  append_json_string(line, kind);
  line.append(",\"name\":");
  append_json_string(line, name);
  line.append(",\"message\":");
  append_json_string(line, message);
  line.append(",\"dur_us\":");
  append_number(line, dur_us);
  line.append("}\n");
  std::lock_guard<std::mutex> lock(ch_mu);
  write_all(ch_fd, line.data(), line.size());
}

#ifdef SEQ_HAS_CLICKHOUSE
void push_ch_row(std::string_view level,
                 std::string_view kind,
                 std::string_view name,
                 std::string_view message,
                 long long dur_us) {
  if (!ch_writer) {
    return;
  }
  seq::ch::TraceEventRow row;
  row.ts_us = now_us();
  row.app = app_name;
  row.pid = static_cast<uint32_t>(::getpid());
  row.tid = thread_id();
  row.level = std::string(level);
  row.kind = std::string(kind);
  row.name = std::string(name);
  row.message = std::string(message);
  row.dur_us = static_cast<int64_t>(dur_us);
  ch_writer->PushTraceEvent(std::move(row));
}
#endif

void emit_ch_row(std::string_view level,
                 std::string_view kind,
                 std::string_view name,
                 std::string_view message,
                 long long dur_us) {
#ifdef SEQ_HAS_CLICKHOUSE
  if (ch_writer) {
    push_ch_row(level, kind, name, message, dur_us);
  }
#endif
  write_ch_row(level, kind, name, message, dur_us);
}
}  // namespace

void init(std::string_view app) {
  if (initialized) {
    return;
  }
  initialized = true;
  app_name = std::string(app);

  std::string log_dir = resolve_log_dir();
  std::error_code ec;
  std::filesystem::create_directories(log_dir, ec);

  cli_fd = open_log(log_dir + "/cli.log");
  trace_fd = open_log(log_dir + "/trace.log");

#ifdef SEQ_HAS_CLICKHOUSE
  ch_mode = parse_ch_mode();
  const bool enable_native = (ch_mode == ChMode::kNative || ch_mode == ChMode::kMirror);
  const bool enable_file = (ch_mode == ChMode::kFile || ch_mode == ChMode::kMirror);
#else
  const bool enable_file = true;
#endif

#ifdef SEQ_HAS_CLICKHOUSE
  if (enable_native) {
    // Reads host/port/db from env, defaulting to localhost:9000/seq.
    seq::ch::Config cfg;
    const char* ch_host = std::getenv("SEQ_CH_HOST");
    if (ch_host && *ch_host) cfg.host = ch_host;
    const char* ch_port = std::getenv("SEQ_CH_PORT");
    if (ch_port && *ch_port) cfg.port = static_cast<uint16_t>(std::atoi(ch_port));
    const char* ch_db = std::getenv("SEQ_CH_DATABASE");
    if (ch_db && *ch_db) cfg.database = ch_db;
    try {
      ch_writer = std::make_unique<seq::ch::AsyncWriter>(std::move(cfg));
    } catch (...) {
      // Best-effort. Mirror mode can still keep local JSON spool.
    }
  }
#endif

  if (enable_file) {
    std::string ch_path = resolve_ch_log_path();
    if (!ch_path.empty()) {
      std::error_code ec2;
      std::filesystem::create_directories(std::filesystem::path(ch_path).parent_path(), ec2);
      ch_fd = open_log(ch_path);
    }
  }

  std::string init_msg = "trace init: " + app_name;
#ifdef SEQ_HAS_CLICKHOUSE
  init_msg.append(" ch_mode=").append(ch_mode_name(ch_mode));
#endif
  write_line(cli_fd, "info", init_msg);
  write_line(trace_fd, "trace", init_msg);
  emit_ch_row("info", "log", "trace.init", init_msg, 0);
}

void shutdown() {
  if (cli_fd >= 0) {
    ::close(cli_fd);
    cli_fd = -1;
  }
  if (trace_fd >= 0) {
    ::close(trace_fd);
    trace_fd = -1;
  }
#ifdef SEQ_HAS_CLICKHOUSE
  ch_writer.reset();  // Flushes and joins background thread
#endif
  if (ch_fd >= 0) {
    ::close(ch_fd);
    ch_fd = -1;
  }
  initialized = false;
}

void log(std::string_view level, std::string_view msg) {
  write_line(cli_fd, level, msg);
  emit_ch_row(level, "log", "", msg, 0);
}

void event(std::string_view name, std::string_view detail) {
  if (detail.empty()) {
    write_line(trace_fd, "event", name);
    emit_ch_row("event", "event", name, "", 0);
    return;
  }
  char buf[256];
  int n = std::snprintf(
      buf,
      sizeof(buf),
      "%.*s %.*s",
      static_cast<int>(name.size()),
      name.data(),
      static_cast<int>(detail.size()),
      detail.data());
  if (n < 0) {
    return;
  }
  write_line(trace_fd, "event", std::string_view(buf, static_cast<size_t>(n)));
  emit_ch_row("event", "event", name, detail, 0);
}

std::string writer_perf_json() {
  const bool file_enabled = (ch_fd >= 0);
#ifdef SEQ_HAS_CLICKHOUSE
  const bool native_enabled = static_cast<bool>(ch_writer);
  std::string out;
  out.reserve(512);
  out.append("{\"enabled\":").append((native_enabled || file_enabled) ? "true" : "false");
  out.append(",\"mode\":\"").append(ch_mode_name(ch_mode)).append("\"");
  out.append(",\"native_enabled\":").append(native_enabled ? "true" : "false");
  out.append(",\"file_enabled\":").append(file_enabled ? "true" : "false");
  if (native_enabled) {
    auto perf = ch_writer->PerfSnapshot();
    out.append(",\"push_calls\":").append(std::to_string(perf.push_calls));
    out.append(",\"wake_count\":").append(std::to_string(perf.wake_count));
    out.append(",\"flush_count\":").append(std::to_string(perf.flush_count));
    out.append(",\"total_flush_us\":").append(std::to_string(perf.total_flush_us));
    out.append(",\"max_flush_us\":").append(std::to_string(perf.max_flush_us));
    out.append(",\"last_flush_us\":").append(std::to_string(perf.last_flush_us));
    out.append(",\"last_flush_rows\":").append(std::to_string(perf.last_flush_rows));
    out.append(",\"last_pending_rows\":").append(std::to_string(perf.last_pending_rows));
    out.append(",\"max_pending_rows\":").append(std::to_string(perf.max_pending_rows));
    out.append(",\"error_count\":").append(std::to_string(perf.error_count));
    out.append(",\"inserted_count\":").append(std::to_string(perf.inserted_count));
    if (perf.flush_count) {
      out.append(",\"avg_flush_us\":").append(std::to_string(perf.total_flush_us / perf.flush_count));
    } else {
      out.append(",\"avg_flush_us\":0");
    }
  } else {
    out.append(",\"push_calls\":0");
    out.append(",\"wake_count\":0");
    out.append(",\"flush_count\":0");
    out.append(",\"total_flush_us\":0");
    out.append(",\"max_flush_us\":0");
    out.append(",\"last_flush_us\":0");
    out.append(",\"last_flush_rows\":0");
    out.append(",\"last_pending_rows\":0");
    out.append(",\"max_pending_rows\":0");
    out.append(",\"error_count\":0");
    out.append(",\"inserted_count\":0");
    out.append(",\"avg_flush_us\":0");
  }
  out.push_back('}');
  return out;
#else
  std::string out;
  out.reserve(128);
  out.append("{\"enabled\":").append(file_enabled ? "true" : "false");
  out.append(",\"mode\":\"file\"");
  out.append(",\"native_enabled\":false");
  out.append(",\"file_enabled\":").append(file_enabled ? "true" : "false");
  out.push_back('}');
  return out;
#endif
}

Span::Span(std::string_view name)
    : name_(name),
      start_(std::chrono::steady_clock::now()) {
  write_line(trace_fd, "span", name_);
  emit_ch_row("trace", "span_start", name_, "", 0);
}

Span::~Span() {
  auto dur = std::chrono::duration_cast<std::chrono::microseconds>(
      std::chrono::steady_clock::now() - start_);
  char buf[160];
  int n = std::snprintf(
      buf,
      sizeof(buf),
      "%.*s dur_us=%lld",
      static_cast<int>(name_.size()),
      name_.data(),
      static_cast<long long>(dur.count()));
  if (n < 0) {
    return;
  }
  write_line(trace_fd, "span", std::string_view(buf, static_cast<size_t>(n)));
  emit_ch_row("trace", "span_end", name_, "", static_cast<long long>(dur.count()));
}

Guard::Guard(std::string_view app) {
  init(app);
}

Guard::~Guard() {
  shutdown();
}
}  // namespace trace
