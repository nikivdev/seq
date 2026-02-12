#include "action_pack_cli.h"

#include "action_pack.h"
#include "action_pack_crypto.h"
#include "io.h"
#include "options.h"
#include "process.h"
#include "strings.h"
#include "trace.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/file.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <fstream>
#include <map>
#include <pwd.h>
#include <string>
#include <string_view>
#include <vector>

namespace {

int send_to(std::string_view addr, const std::vector<uint8_t>& bytes);
bool write_all_fd(int fd, const void* data, size_t len);

uint64_t now_epoch_ms() {
  using namespace std::chrono;
  return (uint64_t)duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

bool read_file(const std::string& path, std::string* out, std::string* error) {
  if (!out) return false;
  std::ifstream in(path, std::ios::binary);
  if (!in.good()) {
    if (error) *error = "unable to open: " + path;
    return false;
  }
  std::string buf((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
  *out = std::move(buf);
  return true;
}

bool write_file(const std::string& path, const std::vector<uint8_t>& data, std::string* error) {
  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  if (!out.good()) {
    if (error) *error = "unable to write: " + path;
    return false;
  }
  out.write(reinterpret_cast<const char*>(data.data()), (std::streamsize)data.size());
  if (!out.good()) {
    if (error) *error = "write failed: " + path;
    return false;
  }
  return true;
}

std::string home_dir() {
  const char* h = std::getenv("HOME");
  if (h && *h) return std::string(h);
  // Some invocations might run with HOME unset.
  struct passwd pwd{};
  struct passwd* result = nullptr;
  char buf[16384];
  if (::getpwuid_r(::getuid(), &pwd, buf, sizeof(buf), &result) == 0 && result &&
      result->pw_dir && *result->pw_dir) {
    return std::string(result->pw_dir);
  }
  return {};
}

void ensure_seq_app_support_dir() {
  std::string home = home_dir();
  if (home.empty()) return;
  std::string d1 = home + "/Library";
  std::string d2 = home + "/Library/Application Support";
  std::string d3 = home + "/Library/Application Support/seq";
  (void)::mkdir(d1.c_str(), 0755);
  (void)::mkdir(d2.c_str(), 0755);
  (void)::mkdir(d3.c_str(), 0755);
}

std::string receivers_path() {
  std::string home = home_dir();
  if (home.empty()) return {};
  return home + "/Library/Application Support/seq/action_pack_receivers";
}

std::map<std::string, std::string> load_receivers() {
  std::map<std::string, std::string> out;
  std::string path = receivers_path();
  if (path.empty()) return out;
  std::ifstream in(path);
  if (!in.good()) return out;
  std::string line;
  while (std::getline(in, line)) {
    std::string trimmed = strings::trim(line);
    if (trimmed.empty()) continue;
    if (!trimmed.empty() && trimmed[0] == '#') continue;
    size_t sp = trimmed.find_first_of(" \t");
    if (sp == std::string::npos) continue;
    std::string name = trimmed.substr(0, sp);
    std::string addr = strings::trim(trimmed.substr(sp + 1));
    if (name.empty() || addr.empty()) continue;
    out[std::move(name)] = std::move(addr);
  }
  return out;
}

struct FileLock {
  int fd = -1;
  explicit FileLock(const std::string& lock_path) {
    fd = ::open(lock_path.c_str(), O_CREAT | O_RDWR, 0600);
    if (fd < 0) return;
    if (::flock(fd, LOCK_EX) != 0) {
      ::close(fd);
      fd = -1;
      return;
    }
  }
  ~FileLock() {
    if (fd >= 0) {
      (void)::flock(fd, LOCK_UN);
      ::close(fd);
    }
  }
  bool ok() const { return fd >= 0; }
};

bool write_text_atomic_0600(const std::string& path, const std::string& data) {
  ensure_seq_app_support_dir();
  size_t slash = path.rfind('/');
  if (slash == std::string::npos || slash == 0) return false;
  std::string dir = path.substr(0, slash);
  std::string tmpl = dir + "/.tmp.XXXXXX";
  std::vector<char> buf(tmpl.begin(), tmpl.end());
  buf.push_back('\0');
  int fd = ::mkstemp(buf.data());
  if (fd < 0) return false;
  bool ok = false;
  do {
    if (!write_all_fd(fd, data.data(), data.size())) break;
    (void)::fchmod(fd, 0600);
    (void)::fsync(fd);
    ::close(fd);
    fd = -1;
    std::string tmp_path(buf.data());
    if (::rename(tmp_path.c_str(), path.c_str()) != 0) {
      (void)::unlink(tmp_path.c_str());
      break;
    }
    int dfd = ::open(dir.c_str(), O_RDONLY);
    if (dfd >= 0) {
      (void)::fsync(dfd);
      ::close(dfd);
    }
    ok = true;
  } while (false);
  if (fd >= 0) ::close(fd);
  return ok;
}

bool save_receivers(const std::map<std::string, std::string>& rs, std::string* error) {
  ensure_seq_app_support_dir();
  std::string path = receivers_path();
  if (path.empty()) {
    if (error) *error = "HOME unavailable; cannot persist receivers";
    return false;
  }
  FileLock lock(path + ".lock");
  if (!lock.ok()) {
    if (error) *error = "unable to lock receivers file";
    return false;
  }
  std::string data;
  for (const auto& kv : rs) {
    data.append(kv.first).push_back(' ');
    data.append(kv.second).push_back('\n');
  }
  if (!write_text_atomic_0600(path, data)) {
    if (error) *error = "unable to write receivers file";
    return false;
  }
  return true;
}

std::string resolve_receiver_addr(std::string_view to) {
  // If it looks like host:port, keep it.
  if (to.find(':') != std::string_view::npos) return std::string(to);
  auto rs = load_receivers();
  auto it = rs.find(std::string(to));
  if (it == rs.end()) return {};
  return it->second;
}

bool parse_host_port(std::string_view s, std::string* host, uint16_t* port, std::string* error) {
  // Supports:
  // - host:port
  // - [ipv6]:port
  std::string h;
  std::string p;
  if (!s.empty() && s.front() == '[') {
    size_t rb = s.find(']');
    if (rb == std::string_view::npos) {
      if (error) *error = "expected [ipv6]:port";
      return false;
    }
    if (rb + 1 >= s.size() || s[rb + 1] != ':') {
      if (error) *error = "expected [ipv6]:port";
      return false;
    }
    h.assign(s.substr(1, rb - 1));
    p.assign(s.substr(rb + 2));
  } else {
    size_t colon = s.rfind(':');
    if (colon == std::string_view::npos) {
      if (error) *error = "expected host:port";
      return false;
    }
    h.assign(s.substr(0, colon));
    p.assign(s.substr(colon + 1));
  }
  if (h.empty() || p.empty()) {
    if (error) *error = "expected host:port";
    return false;
  }
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

std::string default_pubkeys_path() {
  std::string home = home_dir();
  if (home.empty()) return {};
  return home + "/Library/Application Support/seq/action_pack_pubkeys";
}

std::string default_policy_path() {
  std::string home = home_dir();
  if (home.empty()) return {};
  return home + "/Library/Application Support/seq/action_pack.policy";
}

std::string default_receiver_conf_path() {
  std::string home = home_dir();
  if (home.empty()) return {};
  return home + "/Library/Application Support/seq/action_pack_receiver.conf";
}

bool append_line_unique(const std::string& path, std::string_view prefix, const std::string& line) {
  // Replace existing lines that start with prefix; else append.
  FileLock lock(path + ".lock");
  if (!lock.ok()) return false;
  std::vector<std::string> lines;
  {
    std::ifstream in(path);
    std::string s;
    while (std::getline(in, s)) {
      lines.push_back(s);
    }
  }
  bool replaced = false;
  for (auto& s : lines) {
    if (std::string_view(s).rfind(prefix, 0) == 0) {
      s = line;
      replaced = true;
    }
  }
  if (!replaced) {
    lines.push_back(line);
  }
  ensure_seq_app_support_dir();
  std::string data;
  for (const auto& s : lines) {
    data.append(s).push_back('\n');
  }
  return write_text_atomic_0600(path, data);
}

bool write_text(const std::string& path, const std::string& data) {
  return write_text_atomic_0600(path, data);
}

std::string make_default_policy_line(const std::string& key_id) {
  // Strict allowlist for remote Karabiner build/test pack.
  // (Receiver can edit this file later if needed.)
  std::string s;
  s.reserve(512);
  s.append(key_id);
  s.append(" cmd=/usr/bin/git");
  s.append(" cmd=/usr/bin/make");
  s.append(" cmd=/bin/rm");
  s.append(" cmd=/bin/mkdir");
  s.append(" cmd=/bin/bash");
  s.append(" cmd=/usr/bin/python3");
  s.append(" cmd=/usr/bin/xcodebuild");
  s.append(" cmd=/usr/bin/xcrun");
  s.append(" cmd=/usr/bin/clang");
  s.append(" cmd=/usr/bin/clang++");
  s.append(" allow_root_scripts=0");
  s.append(" allow_exec_writes=0");
  return s;
}

int cmd_register_receiver(const std::string& name, const std::string& addr) {
  auto rs = load_receivers();
  rs[name] = addr;
  std::string err;
  if (!save_receivers(rs, &err)) {
    io::err.write("ERR ");
    io::err.write(err);
    io::err.write("\n");
    return 1;
  }
  io::out.write("OK\n");
  return 0;
}

int cmd_pair_receiver(const Options& opts,
                      const std::string& name,
                      const std::string& addr,
                      const std::string& key_id,
                      const std::string& ssh_host) {
  // Ensure key exists and fetch pubkey.
  std::string pub_b64;
  std::string err;
  if (!action_pack_crypto::keygen_p256(key_id, &pub_b64, &err)) {
    io::err.write("ERR ");
    io::err.write(err);
    io::err.write("\n");
    return 1;
  }
  int rc = cmd_register_receiver(name, addr);
  if (rc != 0) return rc;

  // Print a single command the receiver can paste:
  // - bind on all interfaces on that port (`:PORT`)
  // - trust this sender key
  // - deploy (restarts seqd, which auto-loads the receiver conf)
  std::string host;
  uint16_t port = 0;
  if (!parse_host_port(addr, &host, &port, nullptr)) {
    port = 0;
  }
  std::string listen = port ? (":" + std::to_string(port)) : addr;

  std::string receiver_cmd;
  receiver_cmd.reserve(512);
  receiver_cmd.append("cd ~/code/seq && tools/action_pack_receiver_enable.sh --listen ");
  receiver_cmd.append(listen);
  receiver_cmd.append(" --trust ");
  receiver_cmd.append(key_id);
  receiver_cmd.push_back(' ');
  receiver_cmd.append(pub_b64);
  receiver_cmd.append(" && f deploy");

  io::out.write("Receiver command (run on the other Mac):\n");
  io::out.write("  ");
  io::out.write(receiver_cmd);
  io::out.write("\n");

  if (!ssh_host.empty()) {
    io::out.write("\nRunning via tailscale ssh...\n");
    process::CaptureResult r = process::run_capture(
        {"tailscale", "ssh", ssh_host, "--", "/bin/sh", "-lc", receiver_cmd},
        {}, opts.root, 0, 1024 * 1024);
    if (!r.out.empty()) io::out.write(r.out);
    if (!r.err.empty()) io::err.write(r.err);
    if (!r.ok) {
      io::err.write("ERR tailscale ssh failed\n");
      return 1;
    }
  }

  return 0;
}

int cmd_receiver_enable(const std::string& listen,
                        const std::string& trust_key_id,
                        const std::string& pubkey_b64,
                        const std::string& root) {
  const std::string pubkeys = default_pubkeys_path();
  const std::string policy = default_policy_path();
  const std::string conf = default_receiver_conf_path();
  if (pubkeys.empty() || policy.empty() || conf.empty()) {
    io::err.write("ERR HOME unavailable; cannot persist receiver config\n");
    return 1;
  }

  // Write pubkeys entry.
  {
    FileLock lock(pubkeys + ".lock");
    if (!lock.ok()) {
      io::err.write("ERR unable to lock pubkeys\n");
      return 1;
    }
    // Remove any existing entry for key_id; keep other lines.
    std::vector<std::string> lines;
    {
      std::ifstream in(pubkeys);
      std::string s;
      while (std::getline(in, s)) lines.push_back(s);
    }
    std::vector<std::string> out_lines;
    out_lines.reserve(lines.size() + 1);
    for (const auto& s : lines) {
      std::string trimmed = strings::trim(s);
      if (trimmed.rfind(trust_key_id + " ", 0) == 0) continue;
      if (trimmed == trust_key_id) continue;
      out_lines.push_back(s);
    }
    out_lines.push_back(trust_key_id + " " + pubkey_b64);
    std::string data;
    for (const auto& s : out_lines) {
      data.append(s).push_back('\n');
    }
    if (!write_text_atomic_0600(pubkeys, data)) {
      io::err.write("ERR unable to write pubkeys\n");
      return 1;
    }
  }

  // Write/replace policy line for this key_id.
  if (!append_line_unique(policy, trust_key_id + " ", make_default_policy_line(trust_key_id))) {
    io::err.write("ERR unable to write policy\n");
    return 1;
  }

  // Write receiver config. seqd will auto-load this at startup.
  // Allow loopback always; peer filtering still applies.
  bool allow_local = true;
  std::string cfg;
  cfg.reserve(512);
  cfg.append("# seq action-pack receiver config\n");
  cfg.append("listen=").append(listen).append("\n");
  cfg.append("root=").append(root).append("\n");
  cfg.append("pubkeys=").append(pubkeys).append("\n");
  cfg.append("policy=").append(policy).append("\n");
  cfg.append(std::string("allow_local=") + (allow_local ? "1\n" : "0\n"));
  cfg.append("allow_tailscale=1\n");
  cfg.append("max_conns=4\n");
  cfg.append("io_timeout_ms=5000\n");
  cfg.append("max_request=4194304\n");
  cfg.append("max_output=1048576\n");

  if (!write_text(conf, cfg)) {
    io::err.write("ERR unable to write receiver config\n");
    return 1;
  }

  io::out.write("OK\n");
  io::out.write("Next: `cd ~/code/seq && f deploy`.\n");
  return 0;
}

int cmd_karabiner_test(const Options& opts, const std::string& receiver, const std::string& key_id) {
  // Generate the pack locally (sender machine), then send to receiver.
  std::string script = opts.root + "/tools/gen_action_pack_karabiner_test.sh";
  struct stat st{};
  if (::stat(script.c_str(), &st) != 0) {
    io::err.write("ERR missing generator script: ");
    io::err.write(script);
    io::err.write("\n");
    return 1;
  }

  std::unordered_map<std::string, std::string> env;
  env["KEY_ID"] = key_id;

  process::CaptureResult r = process::run_capture({"/bin/bash", script}, env, opts.root, 0, 1024 * 1024);
  if (!r.ok) {
    io::err.write("ERR failed to generate action pack\n");
    if (!r.out.empty()) io::err.write(r.out);
    if (!r.err.empty()) io::err.write(r.err);
    return 1;
  }

  std::string pack_path = opts.root + "/out/action_packs/karabiner_latency_test.sap";
  std::string blob;
  std::string ferr;
  if (!read_file(pack_path, &blob, &ferr)) {
    io::err.write("ERR ");
    io::err.write(ferr);
    io::err.write("\n");
    return 1;
  }
  std::vector<uint8_t> bytes(blob.begin(), blob.end());
  return send_to(receiver, bytes);
}

int send_to(std::string_view addr, const std::vector<uint8_t>& bytes) {
  std::string resolved = resolve_receiver_addr(addr);
  if (resolved.empty()) {
    io::err.write("error: unknown receiver (use: seq action-pack receivers | seq action-pack register <name> <addr>)\n");
    return 1;
  }
  std::string host;
  uint16_t port = 0;
  std::string perr;
  if (!parse_host_port(resolved, &host, &port, &perr)) {
    io::err.write("error: ");
    io::err.write(perr);
    io::err.write("\n");
    return 1;
  }

  char port_s[16];
  std::snprintf(port_s, sizeof(port_s), "%u", (unsigned)port);

  struct addrinfo hints {};
  hints.ai_socktype = SOCK_STREAM;
  hints.ai_family = AF_UNSPEC;
  hints.ai_flags = AI_ADDRCONFIG;

  struct addrinfo* res = nullptr;
  int gai = ::getaddrinfo(host.c_str(), port_s, &hints, &res);
  if (gai != 0 || !res) {
    io::err.write("error: resolve failed\n");
    return 1;
  }

  int fd = -1;
  for (struct addrinfo* ai = res; ai; ai = ai->ai_next) {
    int s = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
    if (s < 0) continue;
    if (::connect(s, ai->ai_addr, (socklen_t)ai->ai_addrlen) == 0) {
      fd = s;
      break;
    }
    ::close(s);
  }
  ::freeaddrinfo(res);
  if (fd < 0) {
    io::err.write("error: connect failed\n");
    return 1;
  }

  if (!write_all_fd(fd, bytes.data(), bytes.size())) {
    io::err.write("error: write failed\n");
    ::close(fd);
    return 1;
  }
  ::shutdown(fd, SHUT_WR);
  std::string resp;
  if (!read_all_fd(fd, &resp, 8 * 1024 * 1024)) {
    io::err.write("error: read failed\n");
    ::close(fd);
    return 1;
  }
  ::close(fd);
  io::out.write(resp);
  if (!resp.empty() && resp.back() != '\n') io::out.write("\n");
  return 0;
}

void print_action_pack_usage() {
  io::out.write("USAGE:\n");
  io::out.write("  seq action-pack pair <name> <addr> [--id <key_id>] [--ssh <tailscale_host>]\n");
  io::out.write("  seq action-pack receiver enable --listen <ip:port> --trust <key_id> <pubkey>\n");
  io::out.write("  seq action-pack register <name> <ip:port>\n");
  io::out.write("  seq action-pack receivers\n");
  io::out.write("  seq action-pack karabiner-test <receiver>\n");
  io::out.write("  seq action-pack keygen [--id <key_id>]\n");
  io::out.write("  seq action-pack export-pub [--id <key_id>]\n");
  io::out.write("  seq action-pack pack <script> --out <pack.sap> [--id <key_id>] [--ttl-ms <n>]\n");
  io::out.write("  seq action-pack send --to <receiver|ip:port> <pack.sap>\n");
  io::out.write("  seq action-pack run --to <receiver|ip:port> <script> [--id <key_id>] [--ttl-ms <n>]\n");
  io::out.write("\n");
  io::out.write("SCRIPT FORMAT:\n");
  io::out.write("  # comments\n");
  io::out.write("  cd /path\n");
  io::out.write("  timeout 600000\n");
  io::out.write("  env KEY=VALUE\n");
  io::out.write("  put /abs/dest @/path/to/local/file\n");
  io::out.write("  exec git status\n");
}

}  // namespace

int cmd_action_pack(int argc, char** argv, int index, const Options& opts) {
  if (index >= argc) {
    print_action_pack_usage();
    return 1;
  }
  std::string_view sub = argv[index++];

  std::string key_id = "default";
  uint64_t ttl_ms = 5 * 60 * 1000;
  std::string out_path;
  std::string to_addr;
  std::string listen;
  std::string root = "/tmp";
  std::string ssh_host;

  auto parse_common_flags = [&]() -> bool {
    while (index < argc) {
      std::string_view a = argv[index];
      if (a == "--id") {
        if (index + 1 >= argc) return false;
        key_id = argv[index + 1];
        index += 2;
        continue;
      }
      if (a == "--ttl-ms") {
        if (index + 1 >= argc) return false;
        char* end = nullptr;
        errno = 0;
        unsigned long long v = std::strtoull(argv[index + 1], &end, 10);
        if (errno != 0 || !end || *end != '\0') return false;
        ttl_ms = (uint64_t)v;
        index += 2;
        continue;
      }
      if (a == "--out") {
        if (index + 1 >= argc) return false;
        out_path = argv[index + 1];
        index += 2;
        continue;
      }
      if (a == "--to") {
        if (index + 1 >= argc) return false;
        to_addr = argv[index + 1];
        index += 2;
        continue;
      }
      if (a == "--listen") {
        if (index + 1 >= argc) return false;
        listen = argv[index + 1];
        index += 2;
        continue;
      }
      if (a == "--root") {
        if (index + 1 >= argc) return false;
        root = argv[index + 1];
        index += 2;
        continue;
      }
      if (a == "--ssh") {
        if (index + 1 >= argc) return false;
        ssh_host = argv[index + 1];
        index += 2;
        continue;
      }
      break;
    }
    return true;
  };

  if (sub == "register") {
    if (index + 1 >= argc) {
      io::err.write("error: register requires <name> <ip:port>\n");
      return 1;
    }
    std::string name = argv[index++];
    std::string addr = argv[index++];
    if (!parse_common_flags() || index < argc) {
      io::err.write("error: bad args\n");
      return 1;
    }
    return cmd_register_receiver(name, addr);
  }

  if (sub == "receivers") {
    if (!parse_common_flags() || index < argc) {
      io::err.write("error: bad args\n");
      return 1;
    }
    auto rs = load_receivers();
    for (const auto& kv : rs) {
      io::out.write(kv.first);
      io::out.write("\t");
      io::out.write(kv.second);
      io::out.write("\n");
    }
    return 0;
  }

  if (sub == "pair") {
    if (index + 1 >= argc) {
      io::err.write("error: pair requires <name> <addr>\n");
      return 1;
    }
    std::string name = argv[index++];
    std::string addr = argv[index++];
    if (!parse_common_flags() || index < argc) {
      io::err.write("error: bad args\n");
      return 1;
    }
    return cmd_pair_receiver(opts, name, addr, key_id, ssh_host);
  }

  if (sub == "receiver") {
    if (index >= argc) {
      io::err.write("error: receiver requires a subcommand\n");
      return 1;
    }
    std::string_view rsub = argv[index++];
    if (rsub == "enable") {
      // receiver enable --listen <ip:port> --trust <key_id> <pubkey> [--root <path>]
      if (!parse_common_flags()) {
        io::err.write("error: bad args\n");
        return 1;
      }
      if (listen.empty()) {
        io::err.write("error: receiver enable requires --listen <ip:port>\n");
        return 1;
      }
      if (index + 1 >= argc) {
        io::err.write("error: receiver enable requires --trust <key_id> <pubkey>\n");
        return 1;
      }
      std::string_view trust_flag = argv[index++];
      if (trust_flag != "--trust") {
        io::err.write("error: receiver enable requires --trust <key_id> <pubkey>\n");
        return 1;
      }
      std::string trust_id = argv[index++];
      std::string pub = argv[index++];
      if (!parse_common_flags() || index < argc) {
        io::err.write("error: bad args\n");
        return 1;
      }
      return cmd_receiver_enable(listen, trust_id, pub, root);
    }
    io::err.write("error: unknown receiver subcommand\n");
    return 1;
  }

  if (sub == "karabiner-test") {
    if (index >= argc) {
      io::err.write("error: karabiner-test requires <receiver>\n");
      return 1;
    }
    std::string receiver = argv[index++];
    if (!parse_common_flags() || index < argc) {
      io::err.write("error: bad args\n");
      return 1;
    }
    return cmd_karabiner_test(opts, receiver, key_id);
  }

  if (sub == "keygen") {
    if (!parse_common_flags()) {
      io::err.write("error: bad args\n");
      return 1;
    }
    std::string pub_b64;
    std::string err;
    if (!action_pack_crypto::keygen_p256(key_id, &pub_b64, &err)) {
      io::err.write("ERR ");
      io::err.write(err);
      io::err.write("\n");
      return 1;
    }
    io::out.write(pub_b64);
    io::out.write("\n");
    return 0;
  }

  if (sub == "export-pub") {
    if (!parse_common_flags()) {
      io::err.write("error: bad args\n");
      return 1;
    }
    std::string pub_b64;
    std::string err;
    if (!action_pack_crypto::export_pubkey_p256(key_id, &pub_b64, &err)) {
      io::err.write("ERR ");
      io::err.write(err);
      io::err.write("\n");
      return 1;
    }
    io::out.write(pub_b64);
    io::out.write("\n");
    return 0;
  }

  if (sub == "pack" || sub == "run") {
    if (!parse_common_flags()) {
      io::err.write("error: bad args\n");
      return 1;
    }
    if (index >= argc) {
      io::err.write("error: missing script path\n");
      return 1;
    }
    std::string script_path = argv[index++];
    // Allow flags after the positional script path as well.
    if (!parse_common_flags()) {
      io::err.write("error: bad args\n");
      return 1;
    }
    if (index < argc) {
      io::err.write("error: unexpected extra args\n");
      return 1;
    }
    if (sub == "pack" && out_path.empty()) {
      io::err.write("error: pack requires --out <file>\n");
      return 1;
    }
    if (sub == "run" && to_addr.empty()) {
      io::err.write("error: run requires --to <receiver|ip:port>\n");
      return 1;
    }

    std::string script;
    std::string ferr;
    if (!read_file(script_path, &script, &ferr)) {
      io::err.write("ERR ");
      io::err.write(ferr);
      io::err.write("\n");
      return 1;
    }

    action_pack::Pack pack;
    std::string perr;
    if (!action_pack::compile_script(script, key_id, now_epoch_ms(), ttl_ms, &pack, &perr)) {
      io::err.write("ERR ");
      io::err.write(perr);
      io::err.write("\n");
      return 1;
    }
    std::vector<uint8_t> payload;
    if (!action_pack::encode_payload(pack, &payload, &perr)) {
      io::err.write("ERR ");
      io::err.write(perr);
      io::err.write("\n");
      return 1;
    }

    std::vector<uint8_t> sig;
    std::string serr;
    if (!action_pack_crypto::sign_p256(key_id,
                                       std::string_view(reinterpret_cast<const char*>(payload.data()), payload.size()),
                                       &sig,
                                       &serr)) {
      io::err.write("ERR ");
      io::err.write(serr);
      io::err.write("\n");
      return 1;
    }

    action_pack::Envelope env;
    env.payload = std::move(payload);
    env.signature = std::move(sig);
    std::vector<uint8_t> bytes;
    if (!action_pack::encode_envelope(env, &bytes, &perr)) {
      io::err.write("ERR ");
      io::err.write(perr);
      io::err.write("\n");
      return 1;
    }

    if (sub == "pack") {
      std::string werr;
      if (!write_file(out_path, bytes, &werr)) {
        io::err.write("ERR ");
        io::err.write(werr);
        io::err.write("\n");
        return 1;
      }
      io::out.write("OK pack_id=");
      io::out.write(action_pack::hex_pack_id(pack.pack_id));
      io::out.write(" bytes=");
      io::out.write(std::to_string(bytes.size()));
      io::out.write("\n");
      return 0;
    }

    return send_to(to_addr, bytes);
  }

  if (sub == "send") {
    if (!parse_common_flags()) {
      io::err.write("error: bad args\n");
      return 1;
    }
    if (to_addr.empty()) {
      io::err.write("error: send requires --to <receiver|ip:port>\n");
      return 1;
    }
    if (index >= argc) {
      io::err.write("error: missing pack path\n");
      return 1;
    }
    std::string pack_path = argv[index++];
    // Allow flags after the positional pack path as well.
    if (!parse_common_flags()) {
      io::err.write("error: bad args\n");
      return 1;
    }
    if (index < argc) {
      io::err.write("error: unexpected extra args\n");
      return 1;
    }
    std::string blob;
    std::string ferr;
    if (!read_file(pack_path, &blob, &ferr)) {
      io::err.write("ERR ");
      io::err.write(ferr);
      io::err.write("\n");
      return 1;
    }
    std::vector<uint8_t> bytes(blob.begin(), blob.end());
    return send_to(to_addr, bytes);
  }

  if (sub == "help") {
    print_action_pack_usage();
    return 0;
  }

  io::err.write("error: unknown action-pack subcommand\n");
  return 1;
}
