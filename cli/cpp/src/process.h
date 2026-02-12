#pragma once

#include <cstddef>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace process {
int run(const std::vector<std::string>& args, std::string* error);
int run_with_input(const std::vector<std::string>& args, std::string_view input, std::string* error);
// Fire-and-forget: spawn a process and return immediately (no waitpid).
int spawn(const std::vector<std::string>& args, std::string* error);

struct CaptureResult {
  int exit_code = 1;
  bool ok = false;        // process exited with code 0
  bool timed_out = false; // killed due to timeout
  std::string out;
  std::string err;
  std::string error;      // human-readable failure detail (spawn/wait/pipe)
};

// Run a process, capture stdout/stderr, and optionally apply:
// - env_add: KEY->VALUE overrides/additions (applies only to this process)
// - cwd: working directory (requires macOS posix_spawn_file_actions_addchdir_np)
// - timeout_ms: 0 => no timeout
// - max_bytes: per-stream cap; extra output is truncated
CaptureResult run_capture(const std::vector<std::string>& args,
                          const std::unordered_map<std::string, std::string>& env_add,
                          std::string_view cwd,
                          uint32_t timeout_ms,
                          size_t max_bytes);
}  // namespace process
