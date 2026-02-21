#include "process.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <spawn.h>
#include <signal.h>
#include <cstring>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

extern char** environ;

namespace process {
namespace {
int run_impl(const std::vector<std::string>& args, std::string_view input, bool use_input, std::string* error) {
  if (args.empty()) {
    if (error) {
      *error = "empty command";
    }
    return 1;
  }

  std::vector<char*> argv;
  argv.reserve(args.size() + 1);
  for (const auto& arg : args) {
    argv.push_back(const_cast<char*>(arg.c_str()));
  }
  argv.push_back(nullptr);

  int pipefd[2] = {-1, -1};
  posix_spawn_file_actions_t actions;
  posix_spawn_file_actions_init(&actions);

  if (use_input) {
    if (pipe(pipefd) != 0) {
      if (error) {
        *error = "failed to create pipe";
      }
      posix_spawn_file_actions_destroy(&actions);
      return 1;
    }
    posix_spawn_file_actions_adddup2(&actions, pipefd[0], STDIN_FILENO);
    posix_spawn_file_actions_addclose(&actions, pipefd[1]);
  }

  pid_t pid = 0;
  int spawn_res = posix_spawnp(&pid, argv[0], &actions, nullptr, argv.data(), environ);
  posix_spawn_file_actions_destroy(&actions);

  if (use_input) {
    ::close(pipefd[0]);
  }

  if (spawn_res != 0) {
    if (use_input) {
      ::close(pipefd[1]);
    }
    if (error) {
      *error = "failed to spawn command";
    }
    return 1;
  }

  if (use_input) {
    size_t offset = 0;
    while (offset < input.size()) {
      ssize_t n = ::write(pipefd[1], input.data() + offset, input.size() - offset);
      if (n > 0) {
        offset += static_cast<size_t>(n);
        continue;
      }
      if (n < 0 && errno == EINTR) {
        continue;
      }
      break;
    }
    ::close(pipefd[1]);
  }

  int status = 0;
  if (waitpid(pid, &status, 0) < 0) {
    if (error) {
      *error = "waitpid failed";
    }
    return 1;
  }
  if (WIFEXITED(status)) {
    int code = WEXITSTATUS(status);
    if (code != 0 && error) {
      *error = "command failed";
    }
    return code;
  }
  if (error) {
    *error = "command terminated";
  }
  return 1;
}
}  // namespace

int run(const std::vector<std::string>& args, std::string* error) {
  return run_impl(args, {}, false, error);
}

int run_with_input(const std::vector<std::string>& args, std::string_view input, std::string* error) {
  return run_impl(args, input, true, error);
}

int spawn(const std::vector<std::string>& args, std::string* error) {
  if (args.empty()) {
    if (error) *error = "empty command";
    return 1;
  }
  std::vector<char*> argv;
  argv.reserve(args.size() + 1);
  for (const auto& arg : args) {
    argv.push_back(const_cast<char*>(arg.c_str()));
  }
  argv.push_back(nullptr);

  posix_spawn_file_actions_t actions;
  posix_spawn_file_actions_init(&actions);

  pid_t pid = 0;
  int spawn_res = posix_spawnp(&pid, argv[0], &actions, nullptr, argv.data(), environ);
  posix_spawn_file_actions_destroy(&actions);

  if (spawn_res != 0) {
    if (error) *error = "failed to spawn command";
    return 1;
  }
  return 0;
}

namespace {
uint64_t now_ms() {
  struct timeval tv {};
  gettimeofday(&tv, nullptr);
  return (uint64_t)tv.tv_sec * 1000ull + (uint64_t)tv.tv_usec / 1000ull;
}

static std::unordered_map<std::string, std::string> parse_env(char** envp) {
  std::unordered_map<std::string, std::string> out;
  if (!envp) return out;
  for (char** p = envp; *p; ++p) {
    std::string_view kv(*p);
    size_t eq = kv.find('=');
    if (eq == std::string_view::npos || eq == 0) continue;
    out.emplace(std::string(kv.substr(0, eq)), std::string(kv.substr(eq + 1)));
  }
  return out;
}

static std::vector<std::string> build_envp(const std::unordered_map<std::string, std::string>& add) {
  auto base = parse_env(environ);
  for (const auto& kv : add) {
    base[kv.first] = kv.second;
  }
  std::vector<std::string> out;
  out.reserve(base.size());
  for (const auto& kv : base) {
    out.push_back(kv.first + "=" + kv.second);
  }
  return out;
}

static void set_nonblock(int fd) {
  int flags = fcntl(fd, F_GETFL, 0);
  if (flags >= 0) {
    (void)fcntl(fd, F_SETFL, flags | O_NONBLOCK);
  }
}
}  // namespace

CaptureResult run_capture(const std::vector<std::string>& args,
                          const std::unordered_map<std::string, std::string>& env_add,
                          std::string_view cwd,
                          uint32_t timeout_ms,
                          size_t max_bytes) {
  CaptureResult res;
  if (args.empty()) {
    res.error = "empty command";
    return res;
  }

  std::vector<char*> argv;
  argv.reserve(args.size() + 1);
  for (const auto& arg : args) {
    argv.push_back(const_cast<char*>(arg.c_str()));
  }
  argv.push_back(nullptr);

  char** spawn_envp = environ;
  std::vector<std::string> env_storage;
  std::vector<char*> envp;
  if (!env_add.empty()) {
    env_storage = build_envp(env_add);
    envp.reserve(env_storage.size() + 1);
    for (auto& s : env_storage) {
      envp.push_back(s.data());
    }
    envp.push_back(nullptr);
    spawn_envp = envp.data();
  }

  int out_pipe[2] = {-1, -1};
  int err_pipe[2] = {-1, -1};
  if (pipe(out_pipe) != 0 || pipe(err_pipe) != 0) {
    if (out_pipe[0] >= 0) { ::close(out_pipe[0]); ::close(out_pipe[1]); }
    if (err_pipe[0] >= 0) { ::close(err_pipe[0]); ::close(err_pipe[1]); }
    res.error = "failed to create pipes";
    return res;
  }

  posix_spawn_file_actions_t actions;
  posix_spawn_file_actions_init(&actions);
  posix_spawn_file_actions_adddup2(&actions, out_pipe[1], STDOUT_FILENO);
  posix_spawn_file_actions_adddup2(&actions, err_pipe[1], STDERR_FILENO);
  posix_spawn_file_actions_addclose(&actions, out_pipe[0]);
  posix_spawn_file_actions_addclose(&actions, err_pipe[0]);
  posix_spawn_file_actions_addclose(&actions, out_pipe[1]);
  posix_spawn_file_actions_addclose(&actions, err_pipe[1]);

#if defined(__APPLE__)
  std::string cwd_str;
  if (!cwd.empty()) {
    // Safer than chdir() around spawn in multi-threaded processes.
    cwd_str.assign(cwd.data(), cwd.size());
    posix_spawn_file_actions_addchdir(&actions, cwd_str.c_str());
  }
#else
  (void)cwd;
#endif

  pid_t pid = 0;
  int spawn_res = posix_spawnp(&pid, argv[0], &actions, nullptr, argv.data(), spawn_envp);
  posix_spawn_file_actions_destroy(&actions);

  ::close(out_pipe[1]);
  ::close(err_pipe[1]);

  if (spawn_res != 0) {
    ::close(out_pipe[0]);
    ::close(err_pipe[0]);
    res.error = std::string("failed to spawn command: ") + std::strerror(spawn_res);
    return res;
  }

  set_nonblock(out_pipe[0]);
  set_nonblock(err_pipe[0]);

  uint64_t start = now_ms();
  bool out_open = true;
  bool err_open = true;
  bool exited = false;

  while (out_open || err_open) {
    // Poll for output or timeout/wait.
    struct pollfd fds[2];
    nfds_t nfds = 0;
    if (out_open) {
      fds[nfds].fd = out_pipe[0];
      fds[nfds].events = POLLIN;
      ++nfds;
    }
    if (err_open) {
      fds[nfds].fd = err_pipe[0];
      fds[nfds].events = POLLIN;
      ++nfds;
    }

    int poll_timeout = 50;
    if (timeout_ms != 0) {
      uint64_t elapsed = now_ms() - start;
      if (elapsed >= timeout_ms) {
        res.timed_out = true;
        break;
      }
      uint64_t remaining = timeout_ms - elapsed;
      poll_timeout = static_cast<int>(std::min<uint64_t>(remaining, 50));
    }

    (void)poll(fds, nfds, poll_timeout);

    auto drain = [&](int fd, std::string* dst, bool* open_flag) {
      if (!*open_flag) return;
      char buf[4096];
      while (true) {
        ssize_t n = ::read(fd, buf, sizeof(buf));
        if (n > 0) {
          size_t want = static_cast<size_t>(n);
          if (dst->size() < max_bytes) {
            size_t room = max_bytes - dst->size();
            size_t take = std::min(room, want);
            dst->append(buf, take);
          }
          continue;
        }
        if (n == 0) {
          *open_flag = false;
          ::close(fd);
          return;
        }
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return;
        *open_flag = false;
        ::close(fd);
        return;
      }
    };

    drain(out_pipe[0], &res.out, &out_open);
    drain(err_pipe[0], &res.err, &err_open);

    if (!exited) {
      int status = 0;
      pid_t w = waitpid(pid, &status, WNOHANG);
      if (w == pid) {
        exited = true;
        if (WIFEXITED(status)) {
          res.exit_code = WEXITSTATUS(status);
        } else {
          res.exit_code = 1;
        }
        // Keep draining until pipes close.
      }
    }
  }

  if (res.timed_out) {
    if (out_open) {
      ::close(out_pipe[0]);
      out_open = false;
    }
    if (err_open) {
      ::close(err_pipe[0]);
      err_open = false;
    }
  }

  if (res.timed_out) {
    // Best-effort kill.
    (void)kill(pid, SIGKILL);
    int status = 0;
    (void)waitpid(pid, &status, 0);  // may fail with ECHILD if already reaped
  } else {
    if (!exited) {
      int status = 0;
      if (waitpid(pid, &status, 0) < 0) {
        res.error = "waitpid failed";
        return res;
      }
      if (WIFEXITED(status)) {
        res.exit_code = WEXITSTATUS(status);
      } else {
        res.exit_code = 1;
      }
    }
  }

  res.ok = (res.exit_code == 0) && !res.timed_out;
  return res;
}
}  // namespace process
