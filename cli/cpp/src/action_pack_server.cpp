#include "action_pack_server.h"

#include "action_pack.h"
#include "action_pack_crypto.h"
#include "base64.h"
#include "process.h"
#include "strings.h"
#include "trace.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

#include <chrono>
#include <cerrno>
#include <cstring>
#include <cstdlib>
#include <fstream>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <unordered_map>
#include <condition_variable>
#include <vector>
#include <unordered_set>
#include <sstream>

namespace action_pack_server {
namespace {

uint64_t now_epoch_ms() {
  using namespace std::chrono;
  return (uint64_t)duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

bool read_all_fd(int fd, std::string* out, size_t max_bytes) {
  if (!out) return false;
  out->clear();
  char buf[8192];
  while (true) {
    ssize_t n = ::read(fd, buf, sizeof(buf));
    if (n > 0) {
      if (out->size() + (size_t)n > max_bytes) {
        return false;
      }
      out->append(buf, (size_t)n);
      continue;
    }
    if (n == 0) return true;
    if (errno == EINTR) continue;
    return false;
  }
}

bool write_all_fd(int fd, const void* data, size_t len) {
  const char* p = static_cast<const char*>(data);
  size_t off = 0;
  while (off < len) {
    ssize_t n = ::write(fd, p + off, len - off);
    if (n > 0) {
      off += (size_t)n;
      continue;
    }
    if (n < 0 && errno == EINTR) continue;
    return false;
  }
  return true;
}

bool parse_listen(std::string_view s, std::string* host, uint16_t* port, std::string* error) {
  // Format: host:port (IPv4 only for now).
  size_t colon = s.rfind(':');
  if (colon == std::string_view::npos) {
    if (error) *error = "expected host:port";
    return false;
  }
  std::string h(s.substr(0, colon));
  std::string p(s.substr(colon + 1));
  // Allow ":port" as shorthand for 0.0.0.0:port.
  if (p.empty()) {
    if (error) *error = "expected host:port";
    return false;
  }
  h = strings::trim(h);
  if (h.empty()) h = "0.0.0.0";
  char* end = nullptr;
  errno = 0;
  unsigned long v = std::strtoul(p.c_str(), &end, 10);
  if (errno != 0 || !end || *end != '\0' || v > 65535ul) {
    if (error) *error = "invalid port";
    return false;
  }
  if (host) *host = h;
  if (port) *port = static_cast<uint16_t>(v);
  return true;
}

// Minimal peer filter: allow localhost, and (optionally) Tailscale IPv4 range 100.64.0.0/10.
bool peer_allowed(const sockaddr_in& peer, const Options& opts) {
  uint32_t addr = ntohl(peer.sin_addr.s_addr);
  uint8_t a = (addr >> 24) & 0xff;
  uint8_t b = (addr >> 16) & 0xff;
  if (opts.action_pack_allow_local) {
    if (a == 127) return true;
  }
  if (opts.action_pack_allow_tailscale) {
    if (a == 100 && b >= 64 && b <= 127) return true;
  }
  return false;
}

std::string default_pubkeys_path() {
  const char* home = std::getenv("HOME");
  if (!home || !*home) return "/tmp/seq_action_pack_pubkeys";
  std::string p = home;
  p += "/Library/Application Support/seq/action_pack_pubkeys";
  return p;
}

std::string default_seen_path() {
  const char* home = std::getenv("HOME");
  if (!home || !*home) return "/tmp/seq_action_pack_seen";
  std::string p = home;
  p += "/Library/Application Support/seq/action_pack_seen";
  return p;
}

void ensure_seq_app_support_dir(void) {
  const char* home = std::getenv("HOME");
  if (!home || !*home) return;
  std::string base = home;
  std::string d1 = base + "/Library";
  std::string d2 = base + "/Library/Application Support";
  std::string d3 = base + "/Library/Application Support/seq";
  (void)::mkdir(d1.c_str(), 0755);
  (void)::mkdir(d2.c_str(), 0755);
  (void)::mkdir(d3.c_str(), 0755);
}

struct ConnLimiter {
  explicit ConnLimiter(int max) : max_(std::max(1, max)) {}

  void acquire(void) {
    std::unique_lock<std::mutex> lock(mu_);
    cv_.wait(lock, [&] { return cur_ < max_; });
    ++cur_;
  }

  void release(void) {
    std::lock_guard<std::mutex> lock(mu_);
    if (cur_ > 0) --cur_;
    cv_.notify_one();
  }

private:
  std::mutex mu_;
  std::condition_variable cv_;
  int max_ = 1;
  int cur_ = 0;
};

struct ConnPermit {
  ConnLimiter* lim = nullptr;
  ~ConnPermit() { if (lim) lim->release(); }
};

struct ServerState {
  std::mutex mu;
  std::unordered_map<std::string, std::string> pubkeys_b64_by_id;
  // Optional hardening policy per key_id.
  struct KeyPolicy {
    std::unordered_set<std::string> allowed_cmds;   // absolute commands
    std::unordered_set<std::string> allowed_env;    // env keys
    bool allow_root_scripts = true;                 // allow executing scripts under root
    bool allow_exec_writes = false;                // allow writing files with execute bits
  };
  std::unordered_map<std::string, KeyPolicy> policy_by_id;
  bool have_policy = false;
  std::unordered_map<std::string, uint64_t> seen_pack_expiry_by_hex;
  std::string seen_path;
};

bool load_pubkeys(const std::string& path,
                  std::unordered_map<std::string, std::string>* out,
                  std::string* error) {
  out->clear();
  std::ifstream in(path);
  if (!in.good()) {
    if (error) *error = "unable to open pubkeys file: " + path;
    return false;
  }
  std::string line;
  while (std::getline(in, line)) {
    std::string trimmed = strings::trim(line);
    if (trimmed.empty()) continue;
    if (!trimmed.empty() && trimmed[0] == '#') continue;
    size_t sp = trimmed.find_first_of(" \t");
    if (sp == std::string::npos) continue;
    std::string key_id = trimmed.substr(0, sp);
    std::string rest = strings::trim(trimmed.substr(sp + 1));
    if (key_id.empty() || rest.empty()) continue;
    (*out)[std::move(key_id)] = std::move(rest);
  }
  return true;
}

bool load_policy(const std::string& path,
                 std::unordered_map<std::string, ServerState::KeyPolicy>* out,
                 std::string* error) {
  out->clear();
  std::ifstream in(path);
  if (!in.good()) {
    if (error) *error = "unable to open policy file: " + path;
    return false;
  }
  std::string line;
  while (std::getline(in, line)) {
    std::string trimmed = strings::trim(line);
    if (trimmed.empty()) continue;
    if (!trimmed.empty() && trimmed[0] == '#') continue;
    std::istringstream iss(trimmed);
    std::string key_id;
    if (!(iss >> key_id)) continue;
    if (key_id.empty()) continue;
    ServerState::KeyPolicy p;
    std::string t;
    while (iss >> t) {
      size_t eq = t.find('=');
      if (eq == std::string::npos || eq == 0) continue;
      std::string k = t.substr(0, eq);
      std::string v = t.substr(eq + 1);
      if (k == "cmd") {
        if (!v.empty()) p.allowed_cmds.insert(v);
        continue;
      }
      if (k == "env") {
        if (!v.empty()) p.allowed_env.insert(v);
        continue;
      }
      if (k == "allow_root_scripts") {
        p.allow_root_scripts = (v == "1" || v == "true" || v == "yes" || v == "on");
        continue;
      }
      if (k == "allow_exec_writes") {
        p.allow_exec_writes = (v == "1" || v == "true" || v == "yes" || v == "on");
        continue;
      }
    }
    (*out)[std::move(key_id)] = std::move(p);
  }
  return true;
}

void load_seen(ServerState* st) {
  std::ifstream in(st->seen_path);
  if (!in.good()) return;
  uint64_t now = now_epoch_ms();
  std::string line;
  while (std::getline(in, line)) {
    // hex \t expires_ms
    std::string trimmed = strings::trim(line);
    if (trimmed.empty()) continue;
    size_t tab = trimmed.find('\t');
    if (tab == std::string::npos) continue;
    std::string hex = trimmed.substr(0, tab);
    std::string exp_s = trimmed.substr(tab + 1);
    char* end = nullptr;
    errno = 0;
    unsigned long long exp = std::strtoull(exp_s.c_str(), &end, 10);
    if (errno != 0 || !end || *end != '\0') continue;
    if (exp != 0 && exp < now) continue;
    st->seen_pack_expiry_by_hex[std::move(hex)] = (uint64_t)exp;
  }
}

void append_seen(ServerState* st, const std::string& hex, uint64_t expires_ms) {
  ensure_seq_app_support_dir();
  std::ofstream out(st->seen_path, std::ios::app);
  if (!out.good()) return;
  out << hex << "\t" << expires_ms << "\n";
}

bool within_root(const std::string& path, const std::string& root) {
  if (root.empty()) return true;
  if (path.empty()) return false;
  // Cheap string-prefix check; callers should pass realpaths for stronger guarantees.
  if (path.size() < root.size()) return false;
  if (path.compare(0, root.size(), root) != 0) return false;
  if (path.size() == root.size()) return true;
  return path[root.size()] == '/';
}

std::string realpath_str(const std::string& p) {
  char buf[PATH_MAX];
  if (::realpath(p.c_str(), buf)) {
    return std::string(buf);
  }
  return {};
}

std::vector<std::string> resolve_argv(const std::vector<std::string>& argv) {
  if (argv.empty()) return {};
  static const std::unordered_map<std::string, std::string> kMap = {
      {"git", "/usr/bin/git"},
      {"make", "/usr/bin/make"},
      {"pwd", "/bin/pwd"},
      {"echo", "/bin/echo"},
      {"ls", "/bin/ls"},
      {"rm", "/bin/rm"},
      {"mkdir", "/bin/mkdir"},
      {"bash", "/bin/bash"},
      {"zsh", "/bin/zsh"},
      {"python3", "/usr/bin/python3"},
      {"xcodebuild", "/usr/bin/xcodebuild"},
      {"clang", "/usr/bin/clang"},
      {"clang++", "/usr/bin/clang++"},
  };
  std::vector<std::string> out = argv;
  auto it = kMap.find(out[0]);
  if (it != kMap.end()) {
    out[0] = it->second;
  }
  return out;
}

std::string expand_vars(std::string_view in) {
  // Minimal expansion: $HOME and ${HOME}, plus leading "~/"
  const char* home_c = std::getenv("HOME");
  std::string home = (home_c && *home_c) ? std::string(home_c) : std::string();
  if (home.empty()) return std::string(in);

  std::string s(in);
  if (s.rfind("~/", 0) == 0) {
    s = home + s.substr(1);
  } else if (s == "~") {
    s = home;
  }

  auto replace_all = [&](const std::string& needle, const std::string& repl) {
    size_t pos = 0;
    while (true) {
      pos = s.find(needle, pos);
      if (pos == std::string::npos) break;
      s.replace(pos, needle.size(), repl);
      pos += repl.size();
    }
  };
  replace_all("${HOME}", home);
  replace_all("$HOME", home);
  return s;
}

bool is_denied_env_key(std::string_view k) {
  auto starts = [&](std::string_view p) { return k.rfind(p, 0) == 0; };
  if (starts("DYLD_")) return true;
  if (starts("LD_")) return true;
  if (k == "DYLD_INSERT_LIBRARIES") return true;
  if (k == "LD_PRELOAD") return true;
  return false;
}

bool cmd_allowed(std::string_view cmd,
                 const Options& opts,
                 const ServerState::KeyPolicy* policy,
                 const std::unordered_set<std::string>& written_canon_paths) {
  // Policy mode: strict allowlist.
  if (policy) {
    if (policy->allowed_cmds.find(std::string(cmd)) != policy->allowed_cmds.end()) {
      return true;
    }
    if (policy->allow_root_scripts && !opts.action_pack_root_real.empty()) {
      std::string c(cmd);
      if (written_canon_paths.find(c) != written_canon_paths.end()) {
        return false;  // refuse to execute files written by this pack
      }
      if (within_root(c, opts.action_pack_root_real)) {
        struct stat st{};
        if (::stat(c.c_str(), &st) == 0 && S_ISREG(st.st_mode) && (st.st_mode & 0111)) {
          return true;
        }
      }
    }
    return false;
  }

  // Default mode: small built-in allowlist + scripts under root.
  static const std::vector<std::string> kAllowed = {
      "/usr/bin/git",
      "/usr/bin/make",
      "/bin/bash",
      "/bin/zsh",
      "/usr/bin/python3",
      "/usr/bin/xcodebuild",
      "/usr/bin/clang",
      "/usr/bin/clang++",
      "/bin/pwd",
      "/bin/echo",
      "/bin/ls",
      "/bin/rm",
      "/bin/mkdir",
      "/usr/bin/xcrun",
      "/usr/bin/codesign",
      "/usr/bin/sw_vers",
      "/usr/bin/uname",
      "/usr/bin/wc",
      "/usr/bin/sed",
      "/usr/bin/tee",
  };
  for (const auto& a : kAllowed) {
    if (cmd == a) return true;
  }
  if (!opts.action_pack_root_real.empty()) {
    std::string c(cmd);
    if (written_canon_paths.find(c) != written_canon_paths.end()) {
      return false;
    }
    if (within_root(c, opts.action_pack_root_real)) {
      struct stat st{};
      if (::stat(c.c_str(), &st) == 0 && S_ISREG(st.st_mode) && (st.st_mode & 0111)) {
        return true;
      }
    }
  }
  return false;
}

bool set_timeouts(int fd, int timeout_ms) {
  if (timeout_ms <= 0) return true;
  struct timeval tv{};
  tv.tv_sec = timeout_ms / 1000;
  tv.tv_usec = (timeout_ms % 1000) * 1000;
  if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv)) != 0) return false;
  if (setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv)) != 0) return false;
  return true;
}

bool safe_write_file(const Options& opts,
                     bool allow_exec_writes,
                     const std::string& path,
                     const std::vector<uint8_t>& data,
                     uint32_t mode,
                     std::string* written_canon_path,
                     std::string* error) {
  if (path.empty() || path[0] != '/') {
    if (error) *error = "path must be absolute";
    return false;
  }
  if (opts.action_pack_root_real.empty()) {
    if (error) *error = "write requires --action-pack-root";
    return false;
  }
  // Ensure parent directory exists and is within root.
  size_t slash = path.rfind('/');
  if (slash == std::string::npos || slash == 0) {
    if (error) *error = "bad path";
    return false;
  }
  std::string parent = path.substr(0, slash);
  std::string base = path.substr(slash + 1);
  if (base.empty() || base.find('/') != std::string::npos) {
    if (error) *error = "bad filename";
    return false;
  }
  std::string parent_rp = realpath_str(parent);
  if (parent_rp.empty() || !within_root(parent_rp, opts.action_pack_root_real)) {
    if (error) *error = "bad parent dir";
    return false;
  }
  std::string canon = parent_rp;
  canon.push_back('/');
  canon.append(base);
  if (!within_root(canon, opts.action_pack_root_real)) {
    if (error) *error = "path outside root";
    return false;
  }

  uint32_t m = mode & 0777u;
  m &= ~07000u;  // clear setuid/setgid/sticky
  if (m == 0) m = 0644;
  if (!allow_exec_writes && (m & 0111u)) {
    if (error) *error = "executable writes forbidden";
    return false;
  }

  // Atomic write: create temp file in same directory, then rename over destination.
  // This avoids leaving partially-written files if we crash or hit I/O errors mid-write.
  std::string tmp = parent_rp;
  tmp.append("/.ap_tmp.XXXXXX");
  std::vector<char> tmpl(tmp.begin(), tmp.end());
  tmpl.push_back('\0');
  int fd = ::mkstemp(tmpl.data());
  if (fd < 0) {
    if (error) *error = std::string("mkstemp failed: ") + std::strerror(errno);
    return false;
  }

  std::string tmp_path(tmpl.data());
  bool ok = false;
  do {
    struct stat st{};
    if (::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
      if (error) *error = "temp is not a regular file";
      break;
    }
    if (!data.empty() && !write_all_fd(fd, data.data(), data.size())) {
      if (error) *error = "write failed";
      break;
    }
    (void)::fchmod(fd, (mode_t)m);
    (void)::fsync(fd);
    ::close(fd);
    fd = -1;

    // Refuse to replace non-file destination (best-effort; rename would fail anyway).
    struct stat dst{};
    if (::lstat(canon.c_str(), &dst) == 0) {
      if (S_ISDIR(dst.st_mode)) {
        if (error) *error = "destination is a directory";
        break;
      }
    }

    if (::rename(tmp_path.c_str(), canon.c_str()) != 0) {
      if (error) *error = std::string("rename failed: ") + std::strerror(errno);
      break;
    }
    // Best-effort fsync of parent directory for durability.
    int dfd = ::open(parent_rp.c_str(), O_RDONLY);
    if (dfd >= 0) {
      (void)::fsync(dfd);
      ::close(dfd);
    }

    ok = true;
  } while (false);

  if (fd >= 0) ::close(fd);
  if (!ok) {
    (void)::unlink(tmp_path.c_str());
    return false;
  }

  if (written_canon_path) {
    *written_canon_path = canon;
  }
  return true;
}

std::string handle_pack(ServerState* st, const Options& opts, const action_pack::Envelope& env) {
  // Parse payload first (for key_id/pack_id/etc).
  action_pack::Pack pack;
  std::string perr;
  if (!action_pack::decode_payload(std::string_view(
          reinterpret_cast<const char*>(env.payload.data()), env.payload.size()),
      &pack, &perr)) {
    return "ERR bad payload: " + perr + "\n";
  }

  std::string pack_hex = action_pack::hex_pack_id(pack.pack_id);

  const ServerState::KeyPolicy* policy = nullptr;
  if (st->have_policy) {
    std::lock_guard<std::mutex> lock(st->mu);
    auto it = st->policy_by_id.find(pack.key_id);
    if (it != st->policy_by_id.end()) {
      policy = &it->second;
    } else {
      return "ERR policy missing for key_id: " + pack.key_id + "\n";
    }
  }

  // Lookup key.
  std::string pub_b64;
  {
    std::lock_guard<std::mutex> lock(st->mu);
    auto it = st->pubkeys_b64_by_id.find(pack.key_id);
    if (it == st->pubkeys_b64_by_id.end()) {
      return "ERR unknown key_id: " + pack.key_id + "\n";
    }
    pub_b64 = it->second;
  }

  // Verify signature.
  std::string verr;
  std::string_view payload_view(reinterpret_cast<const char*>(env.payload.data()), env.payload.size());
  std::string_view sig_view(reinterpret_cast<const char*>(env.signature.data()), env.signature.size());
  if (!action_pack_crypto::verify_p256(pub_b64, payload_view, sig_view, &verr)) {
    trace::event("action_pack.verify.fail", pack_hex);
    return "ERR signature invalid: " + verr + "\n";
  }

  uint64_t now = now_epoch_ms();
  // Basic time checks. Allow small skew.
  const uint64_t skew_ms = 30'000;
  if (pack.created_ms != 0 && pack.created_ms > now + skew_ms) {
    return "ERR created_ms in future\n";
  }
  if (pack.expires_ms != 0 && now > pack.expires_ms + skew_ms) {
    return "ERR pack expired\n";
  }

  // Replay check.
  {
    std::lock_guard<std::mutex> lock(st->mu);
    auto it = st->seen_pack_expiry_by_hex.find(pack_hex);
    if (it != st->seen_pack_expiry_by_hex.end()) {
      uint64_t exp = it->second;
      if (exp == 0 || exp > now) {
        return "ERR replay\n";
      }
      // Expired entry; allow reuse but overwrite.
      st->seen_pack_expiry_by_hex.erase(it);
    }
    uint64_t exp = pack.expires_ms;
    st->seen_pack_expiry_by_hex[pack_hex] = exp;
    append_seen(st, pack_hex, exp);
  }

  // Execute.
  std::string resp;
  resp.reserve(1024);
  resp.append("OK pack_id=").append(pack_hex).append(" steps=").append(std::to_string(pack.steps.size())).append("\n");

  std::unordered_map<std::string, std::string> env_add;
  env_add.reserve(pack.env.size());
  for (const auto& kv : pack.env) {
    if (policy) {
      if (policy->allowed_env.find(kv.first) == policy->allowed_env.end()) {
        continue;
      }
    }
    if (is_denied_env_key(kv.first)) {
      continue;
    }
    env_add.emplace(kv.first, kv.second);
  }
  const size_t max_bytes = opts.action_pack_max_output_bytes ? opts.action_pack_max_output_bytes : (256 * 1024);

  std::unordered_set<std::string> written_canon_paths;
  for (size_t i = 0; i < pack.steps.size(); ++i) {
    const auto& stp = pack.steps[i];
    if (const auto* w = std::get_if<action_pack::WriteFileStep>(&stp)) {
      std::string werr;
      std::string canon;
      bool allow_exec_writes = policy ? policy->allow_exec_writes : false;
      std::string path = expand_vars(w->path);
      if (!safe_write_file(opts, allow_exec_writes, path, w->data, w->mode, &canon, &werr)) {
        resp.append("STEP ").append(std::to_string(i)).append(" write ERR ").append(werr).append("\n");
        break;
      }
      if (!canon.empty()) written_canon_paths.insert(canon);
      resp.append("STEP ").append(std::to_string(i)).append(" write OK bytes=")
          .append(std::to_string(w->data.size())).append(" path=").append(path).append("\n");
      continue;
    }
    const auto* step = std::get_if<action_pack::ExecStep>(&stp);
    if (!step) {
      resp.append("STEP ").append(std::to_string(i)).append(" ERR unknown step type\n");
      break;
    }
    if (step->argv.empty()) {
      resp.append("STEP ").append(std::to_string(i)).append(" ERR empty argv\n");
      break;
    }
    std::vector<std::string> argv = resolve_argv(step->argv);
    for (auto& a : argv) {
      a = expand_vars(a);
    }

    std::string cwd = step->cwd.empty() ? opts.action_pack_root : step->cwd;
    cwd = expand_vars(cwd);
    if (!cwd.empty()) {
      std::string rp = realpath_str(cwd);
      if (rp.empty()) {
        resp.append("STEP ").append(std::to_string(i)).append(" ERR bad_cwd\n");
        break;
      }
      if (!within_root(rp, opts.action_pack_root_real)) {
        resp.append("STEP ").append(std::to_string(i)).append(" ERR cwd_outside_root\n");
        break;
      }
      cwd = rp;
    }

    // Resolve command:
    // - absolute (/usr/bin/git), or
    // - relative containing '/' (./tools/foo) which resolves within cwd/root, or
    // - a known short name (mapped by resolve_argv above).
    std::string cmd = argv[0];
    if (!cmd.empty() && cmd[0] != '/') {
      if (cmd.find('/') != std::string::npos) {
        if (cwd.empty() || opts.action_pack_root_real.empty()) {
          resp.append("STEP ").append(std::to_string(i)).append(" ERR relative_cmd_requires_root\n");
          break;
        }
        std::string joined = cwd;
        joined.push_back('/');
        joined.append(cmd);
        std::string rp = realpath_str(joined);
        if (rp.empty()) {
          resp.append("STEP ").append(std::to_string(i)).append(" ERR bad_cmd_path\n");
          break;
        }
        if (!within_root(rp, opts.action_pack_root_real)) {
          resp.append("STEP ").append(std::to_string(i)).append(" ERR cmd_outside_root\n");
          break;
        }
        argv[0] = rp;
        cmd = rp;
      } else {
        resp.append("STEP ").append(std::to_string(i)).append(" ERR cmd_not_allowed\n");
        break;
      }
    }

    if (!cmd_allowed(cmd, opts, policy, written_canon_paths)) {
      resp.append("STEP ").append(std::to_string(i)).append(" ERR cmd_not_allowed\n");
      break;
    }

    auto t0 = std::chrono::steady_clock::now();
    process::CaptureResult r = process::run_capture(argv, env_add, cwd, step->timeout_ms, max_bytes);
    auto t1 = std::chrono::steady_clock::now();
    uint64_t dur_ms = (uint64_t)std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();

    resp.append("STEP ").append(std::to_string(i)).append(" exec exit=").append(std::to_string(r.exit_code));
    resp.append(" dur_ms=").append(std::to_string(dur_ms));
    if (r.timed_out) resp.append(" timed_out=1");
    if (!r.error.empty()) {
      resp.append(" error=").append(r.error);
    }
    resp.push_back('\n');
    if (!r.out.empty()) {
      resp.append("--- STDOUT (").append(std::to_string(r.out.size())).append(" bytes) ---\n");
      resp.append(r.out);
      if (resp.empty() || resp.back() != '\n') resp.push_back('\n');
    }
    if (!r.err.empty()) {
      resp.append("--- STDERR (").append(std::to_string(r.err.size())).append(" bytes) ---\n");
      resp.append(r.err);
      if (resp.empty() || resp.back() != '\n') resp.push_back('\n');
    }
    if (!r.ok) {
      break;
    }
  }

  return resp;
}

void serve_loop(const Options& opts) {
  TRACE_SCOPE("action_pack_server");
  std::string host;
  uint16_t port = 0;
  std::string err;
  if (!parse_listen(opts.action_pack_listen, &host, &port, &err)) {
    trace::log("error", "action-pack listen parse failed: " + err);
    return;
  }

  ServerState st;
  st.seen_path = opts.action_pack_seen_path.empty() ? default_seen_path() : opts.action_pack_seen_path;

  std::string pub_path = opts.action_pack_pubkeys_path.empty() ? default_pubkeys_path() : opts.action_pack_pubkeys_path;
  {
    std::string e;
    if (!load_pubkeys(pub_path, &st.pubkeys_b64_by_id, &e)) {
      trace::log("error", "action-pack pubkeys load failed: " + e);
      return;
    }
    trace::log("info", "action-pack pubkeys loaded: " + pub_path);
  }
  if (!opts.action_pack_policy_path.empty()) {
    std::string e;
    if (!load_policy(opts.action_pack_policy_path, &st.policy_by_id, &e)) {
      trace::log("error", "action-pack policy load failed: " + e);
      return;
    }
    st.have_policy = true;
    trace::log("info", "action-pack policy loaded: " + opts.action_pack_policy_path);
  }
  load_seen(&st);

  // Resolve root realpath once.
  Options local = opts;
  if (local.action_pack_root.empty()) {
    trace::log("error", "action-pack requires --action-pack-root (refuse to run without sandbox)");
    return;
  }
  local.action_pack_root_real = realpath_str(local.action_pack_root);
  if (local.action_pack_root_real.empty()) {
    trace::log("error", "action-pack root realpath failed");
    return;
  }

  ConnLimiter limiter(local.action_pack_max_conns);

  int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) {
    trace::log("error", "action-pack socket failed");
    return;
  }
  int yes = 1;
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
    trace::log("error", "action-pack bad host ip");
    ::close(fd);
    return;
  }
  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    trace::log("error", std::string("action-pack bind failed errno=") + std::to_string(errno));
    ::close(fd);
    return;
  }
  if (::listen(fd, 16) != 0) {
    trace::log("error", "action-pack listen failed");
    ::close(fd);
    return;
  }

  trace::log("info", "action-pack listening " + host + ":" + std::to_string(port));

  while (true) {
    sockaddr_in peer{};
    socklen_t peer_len = sizeof(peer);
    int client = ::accept(fd, reinterpret_cast<sockaddr*>(&peer), &peer_len);
    if (client < 0) {
      if (errno == EINTR) continue;
      trace::log("error", "action-pack accept failed");
      break;
    }

    if (!peer_allowed(peer, local)) {
      ::close(client);
      continue;
    }

    limiter.acquire();
    std::thread([client, &st, &limiter, local]() mutable {
      ConnPermit permit;
      permit.lim = &limiter;

      (void)set_timeouts(client, local.action_pack_io_timeout_ms);
      std::string req;
      bool ok = read_all_fd(client, &req, local.action_pack_max_request_bytes);
      if (!ok) {
        std::string resp = "ERR read_failed\n";
        (void)write_all_fd(client, resp.data(), resp.size());
        ::close(client);
        return;
      }
      std::string derr;
      action_pack::Envelope env;
      if (!action_pack::decode_envelope(req, &env, &derr)) {
        std::string resp = "ERR bad envelope: " + derr + "\n";
        (void)write_all_fd(client, resp.data(), resp.size());
        ::close(client);
        return;
      }
      std::string resp = handle_pack(&st, local, env);
      (void)write_all_fd(client, resp.data(), resp.size());
      ::close(client);
    }).detach();
  }
  ::close(fd);
}

}  // namespace

void start_in_background(const Options& opts) {
  if (opts.action_pack_listen.empty()) return;
  std::thread([opts] { serve_loop(opts); }).detach();
}

}  // namespace action_pack_server
