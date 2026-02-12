#include "options.h"

#include "io.h"

#include <cerrno>
#include <cstdlib>
#include <string_view>

namespace {
constexpr const char* kDefaultSocket = "/tmp/seqd.sock";
constexpr const char* kDefaultRoot = "/Users/nikiv/code/seq";
constexpr const char* kDefaultMacros = "/Users/nikiv/code/seq/seq.macros.yaml";
}  // namespace

Options default_options() {
  Options opts;
  opts.socket_path = kDefaultSocket;
  opts.root = kDefaultRoot;
  opts.macros = kDefaultMacros;
  return opts;
}

static bool arg_matches(std::string_view arg, std::string_view flag) {
  return arg == flag;
}

bool parse_options(int argc, char** argv, int* index, Options* out) {
  while (*index < argc) {
    std::string_view arg = argv[*index];
    if (arg.empty() || arg[0] != '-') {
      return true;
    }
    auto require_value = [&](const char* flag) -> const char* {
      if (*index + 1 >= argc) {
        io::err.write("error: ");
        io::err.write(flag);
        io::err.write(" requires a value\n");
        return nullptr;
      }
      return argv[*index + 1];
    };
    auto parse_bool = [&](std::string_view v, bool* out_bool) -> bool {
      if (!out_bool) return false;
      if (v == "1" || v == "true" || v == "yes" || v == "on") {
        *out_bool = true;
        return true;
      }
      if (v == "0" || v == "false" || v == "no" || v == "off") {
        *out_bool = false;
        return true;
      }
      return false;
    };
    if (arg_matches(arg, "--socket")) {
      const char* v = require_value("--socket");
      if (!v) return false;
      out->socket_path = v;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--root")) {
      const char* v = require_value("--root");
      if (!v) return false;
      out->root = v;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--macros")) {
      const char* v = require_value("--macros");
      if (!v) return false;
      out->macros = v;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-listen")) {
      const char* v = require_value("--action-pack-listen");
      if (!v) return false;
      out->action_pack_listen = v;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-pubkeys")) {
      const char* v = require_value("--action-pack-pubkeys");
      if (!v) return false;
      out->action_pack_pubkeys_path = v;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-policy")) {
      const char* v = require_value("--action-pack-policy");
      if (!v) return false;
      out->action_pack_policy_path = v;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-seen")) {
      const char* v = require_value("--action-pack-seen");
      if (!v) return false;
      out->action_pack_seen_path = v;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-root")) {
      const char* v = require_value("--action-pack-root");
      if (!v) return false;
      out->action_pack_root = v;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-allow-local")) {
      const char* v = require_value("--action-pack-allow-local");
      if (!v) return false;
      bool b = true;
      if (!parse_bool(v, &b)) {
        io::err.write("error: --action-pack-allow-local expects 0/1\n");
        return false;
      }
      out->action_pack_allow_local = b;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-allow-tailscale")) {
      const char* v = require_value("--action-pack-allow-tailscale");
      if (!v) return false;
      bool b = true;
      if (!parse_bool(v, &b)) {
        io::err.write("error: --action-pack-allow-tailscale expects 0/1\n");
        return false;
      }
      out->action_pack_allow_tailscale = b;
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-max-output")) {
      const char* v = require_value("--action-pack-max-output");
      if (!v) return false;
      char* end = nullptr;
      errno = 0;
      unsigned long long n = std::strtoull(v, &end, 10);
      if (errno != 0 || !end || *end != '\0') {
        io::err.write("error: --action-pack-max-output expects an integer\n");
        return false;
      }
      out->action_pack_max_output_bytes = static_cast<size_t>(n);
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-max-request")) {
      const char* v = require_value("--action-pack-max-request");
      if (!v) return false;
      char* end = nullptr;
      errno = 0;
      unsigned long long n = std::strtoull(v, &end, 10);
      if (errno != 0 || !end || *end != '\0') {
        io::err.write("error: --action-pack-max-request expects an integer\n");
        return false;
      }
      out->action_pack_max_request_bytes = static_cast<size_t>(n);
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-max-conns")) {
      const char* v = require_value("--action-pack-max-conns");
      if (!v) return false;
      int n = std::atoi(v);
      out->action_pack_max_conns = std::max(1, n);
      *index += 2;
      continue;
    }
    if (arg_matches(arg, "--action-pack-io-timeout-ms")) {
      const char* v = require_value("--action-pack-io-timeout-ms");
      if (!v) return false;
      int n = std::atoi(v);
      out->action_pack_io_timeout_ms = std::max(100, n);
      *index += 2;
      continue;
    }
    return true;
  }
  return true;
}
