#pragma once

#include <string>

struct Options {
  std::string socket_path;
  std::string root;
  std::string macros;

  // Action pack server (disabled by default).
  // When enabled, seqd listens on a TCP address (IPv4) and accepts signed action packs.
  std::string action_pack_listen;       // e.g. "100.64.0.1:52123"
  std::string action_pack_pubkeys_path; // key_id<ws>base64(pubkey_external)
  std::string action_pack_policy_path;  // optional key_id policy file
  std::string action_pack_seen_path;    // replay cache (append-only; pruned on load)
  std::string action_pack_root;         // optional allowed working-dir root
  std::string action_pack_root_real;    // internal: realpath(root)
  bool action_pack_allow_local = true;  // allow 127.0.0.1
  bool action_pack_allow_tailscale = true; // allow 100.64.0.0/10
  size_t action_pack_max_output_bytes = 256 * 1024; // per stream
  size_t action_pack_max_request_bytes = 4 * 1024 * 1024;
  int action_pack_max_conns = 8;
  int action_pack_io_timeout_ms = 5000; // read/write socket timeouts
};

Options default_options();

bool parse_options(int argc, char** argv, int* index, Options* out);
