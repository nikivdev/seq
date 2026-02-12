#pragma once

#include <cstdint>
#include <array>
#include <string>
#include <string_view>
#include <unordered_map>
#include <variant>
#include <vector>

namespace action_pack {

// High-level representation for pack authoring.
struct ExecStep {
  std::vector<std::string> argv;  // argv[0] must be an executable (prefer absolute)
  std::string cwd;                // empty => inherit server default
  uint32_t timeout_ms = 0;        // 0 => no timeout
};

struct WriteFileStep {
  std::string path;               // absolute path on receiver
  std::vector<uint8_t> data;      // raw bytes
  uint32_t mode = 0644;           // applied after write (masked to 0777)
};

using Step = std::variant<ExecStep, WriteFileStep>;

struct Pack {
  std::string key_id;     // selects a verifier key on the receiving side
  uint64_t created_ms = 0;
  uint64_t expires_ms = 0;
  std::array<uint8_t, 16> pack_id{};  // random; used for replay protection

  // Pack-wide environment additions (applies to all steps).
  std::unordered_map<std::string, std::string> env;

  std::vector<Step> steps;
};

// Binary payload encoding (signed).
bool encode_payload(const Pack& pack, std::vector<uint8_t>* out, std::string* error);
bool decode_payload(std::string_view payload, Pack* out, std::string* error);

// Transport envelope:
//   "SAP1" + u32(payload_len) + payload + u32(sig_len) + sig
struct Envelope {
  std::vector<uint8_t> payload;
  std::vector<uint8_t> signature;
};

bool encode_envelope(const Envelope& env, std::vector<uint8_t>* out, std::string* error);
bool decode_envelope(std::string_view bytes, Envelope* out, std::string* error);

// Simple script format (one instruction per line; no shell evaluation):
//   - Blank lines / lines starting with '#' are ignored
//   - "cd <path>" sets cwd for subsequent execs
//   - "timeout <ms>" sets timeout for subsequent execs
//   - "env KEY=VALUE" sets/overwrites env for the pack
//   - "exec <arg0> <arg1> ..." appends an ExecStep
//   - "put <dest_abs_path> @<src_path>" embeds a local file into the pack
bool compile_script(std::string_view script,
                    const std::string& key_id,
                    uint64_t now_ms,
                    uint64_t ttl_ms,
                    Pack* out,
                    std::string* error);

// Helpers
std::string hex_pack_id(const std::array<uint8_t, 16>& id);
bool parse_hex_pack_id(std::string_view hex, std::array<uint8_t, 16>* out);
std::array<uint8_t, 16> random_pack_id();

}  // namespace action_pack
