#pragma once

#include <chrono>
#include <string>
#include <string_view>

namespace trace {
void init(std::string_view app);
void shutdown();
void log(std::string_view level, std::string_view msg);
void event(std::string_view name, std::string_view detail = {});
std::string writer_perf_json();

struct Span {
  explicit Span(std::string_view name);
  ~Span();

 private:
  std::string_view name_;
  std::chrono::steady_clock::time_point start_;
};

struct Guard {
  explicit Guard(std::string_view app);
  ~Guard();
};
}  // namespace trace

#define TRACE_SCOPE(name) ::trace::Span trace_span_##__COUNTER__(name)
