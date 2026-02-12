#include "trace.h"

#include <cerrno>
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
// Native ClickHouse writer replaces the old ch_fd + JSON file-append path.
std::unique_ptr<seq::ch::AsyncWriter> ch_writer;
#else
int ch_fd = -1;
std::mutex ch_mu;
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
#else
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
#endif
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
  // Create async writer for native ClickHouse protocol.
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
    // Best-effort. If ClickHouse isn't running, traces still go to log files.
  }
  push_ch_row("info", "log", "trace.init", "trace init: " + app_name, 0);
#else
  std::string ch_path = resolve_ch_log_path();
  if (!ch_path.empty()) {
    std::error_code ec2;
    std::filesystem::create_directories(std::filesystem::path(ch_path).parent_path(), ec2);
    ch_fd = open_log(ch_path);
  }
  write_ch_row("info", "log", "trace.init", "trace init: " + app_name, 0);
#endif

  write_line(cli_fd, "info", "trace init: " + app_name);
  write_line(trace_fd, "trace", "trace init: " + app_name);
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
#else
  if (ch_fd >= 0) {
    ::close(ch_fd);
    ch_fd = -1;
  }
#endif
  initialized = false;
}

void log(std::string_view level, std::string_view msg) {
  write_line(cli_fd, level, msg);
#ifdef SEQ_HAS_CLICKHOUSE
  push_ch_row(level, "log", "", msg, 0);
#else
  write_ch_row(level, "log", "", msg, 0);
#endif
}

void event(std::string_view name, std::string_view detail) {
  if (detail.empty()) {
    write_line(trace_fd, "event", name);
#ifdef SEQ_HAS_CLICKHOUSE
    push_ch_row("event", "event", name, "", 0);
#else
    write_ch_row("event", "event", name, "", 0);
#endif
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
#ifdef SEQ_HAS_CLICKHOUSE
  push_ch_row("event", "event", name, detail, 0);
#else
  write_ch_row("event", "event", name, detail, 0);
#endif
}

Span::Span(std::string_view name)
    : name_(name),
      start_(std::chrono::steady_clock::now()) {
  write_line(trace_fd, "span", name_);
#ifdef SEQ_HAS_CLICKHOUSE
  push_ch_row("trace", "span_start", name_, "", 0);
#else
  write_ch_row("trace", "span_start", name_, "", 0);
#endif
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
#ifdef SEQ_HAS_CLICKHOUSE
  push_ch_row("trace", "span_end", name_, "", static_cast<long long>(dur.count()));
#else
  write_ch_row("trace", "span_end", name_, "", static_cast<long long>(dur.count()));
#endif
}

Guard::Guard(std::string_view app) {
  init(app);
}

Guard::~Guard() {
  shutdown();
}
}  // namespace trace
