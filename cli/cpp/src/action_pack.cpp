#include "action_pack.h"

#include "strings.h"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <iterator>

#include <stdlib.h>

namespace action_pack {
namespace {

constexpr uint8_t kPayloadVersion = 2;
constexpr uint32_t kMaxPayloadSteps = 10000;
constexpr size_t kMaxTotalWriteBytes = 8 * 1024 * 1024;

void write_u8(std::vector<uint8_t>* out, uint8_t v) {
  out->push_back(v);
}

void write_u16_le(std::vector<uint8_t>* out, uint16_t v) {
  out->push_back(static_cast<uint8_t>(v & 0xff));
  out->push_back(static_cast<uint8_t>((v >> 8) & 0xff));
}

void write_u32_le(std::vector<uint8_t>* out, uint32_t v) {
  out->push_back(static_cast<uint8_t>(v & 0xff));
  out->push_back(static_cast<uint8_t>((v >> 8) & 0xff));
  out->push_back(static_cast<uint8_t>((v >> 16) & 0xff));
  out->push_back(static_cast<uint8_t>((v >> 24) & 0xff));
}

void write_u64_le(std::vector<uint8_t>* out, uint64_t v) {
  for (int i = 0; i < 8; ++i) {
    out->push_back(static_cast<uint8_t>((v >> (8 * i)) & 0xff));
  }
}

bool write_bytes(std::vector<uint8_t>* out, std::string_view s, std::string* error) {
  if (s.size() > 0xffffu) {
    if (error) *error = "string too long";
    return false;
  }
  write_u16_le(out, static_cast<uint16_t>(s.size()));
  out->insert(out->end(), s.begin(), s.end());
  return true;
}

bool write_blob(std::vector<uint8_t>* out, const std::vector<uint8_t>& data, std::string* error) {
  if (data.size() > 0xffffffffu) {
    if (error) *error = "blob too large";
    return false;
  }
  write_u32_le(out, static_cast<uint32_t>(data.size()));
  out->insert(out->end(), data.begin(), data.end());
  return true;
}

struct Reader {
  const uint8_t* p = nullptr;
  size_t n = 0;

  bool need(size_t k) const { return k <= n; }
  bool read_u8(uint8_t* out) {
    if (!need(1)) return false;
    *out = *p;
    p += 1;
    n -= 1;
    return true;
  }
  bool read_u16_le(uint16_t* out) {
    if (!need(2)) return false;
    *out = static_cast<uint16_t>(p[0] | (static_cast<uint16_t>(p[1]) << 8));
    p += 2;
    n -= 2;
    return true;
  }
  bool read_u32_le(uint32_t* out) {
    if (!need(4)) return false;
    *out = static_cast<uint32_t>(p[0]) |
           (static_cast<uint32_t>(p[1]) << 8) |
           (static_cast<uint32_t>(p[2]) << 16) |
           (static_cast<uint32_t>(p[3]) << 24);
    p += 4;
    n -= 4;
    return true;
  }
  bool read_u64_le(uint64_t* out) {
    if (!need(8)) return false;
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) {
      v |= (static_cast<uint64_t>(p[i]) << (8 * i));
    }
    *out = v;
    p += 8;
    n -= 8;
    return true;
  }
  bool read_bytes(std::string* out) {
    uint16_t len = 0;
    if (!read_u16_le(&len)) return false;
    if (!need(len)) return false;
    out->assign(reinterpret_cast<const char*>(p), len);
    p += len;
    n -= len;
    return true;
  }
  bool read_blob(std::vector<uint8_t>* out) {
    uint32_t len = 0;
    if (!read_u32_le(&len)) return false;
    if (!need(len)) return false;
    out->assign(p, p + len);
    p += len;
    n -= len;
    return true;
  }
  bool skip(size_t k) {
    if (!need(k)) return false;
    p += k;
    n -= k;
    return true;
  }
};

std::vector<std::string> split_ws(std::string_view line) {
  std::vector<std::string> out;
  std::string cur;
  bool in_quote = false;
  char quote = 0;

  auto flush = [&]() {
    if (!cur.empty()) {
      out.emplace_back(std::move(cur));
      cur.clear();
    }
  };

  for (size_t i = 0; i < line.size(); ++i) {
    char c = line[i];
    if (!in_quote && (c == ' ' || c == '\t')) {
      flush();
      continue;
    }
    if (!in_quote && (c == '"' || c == '\'')) {
      in_quote = true;
      quote = c;
      continue;
    }
    if (in_quote && c == quote) {
      in_quote = false;
      quote = 0;
      continue;
    }
    if (c == '\\') {
      // Minimal escaping: treat next char as literal (in or out of quotes).
      if (i + 1 < line.size()) {
        cur.push_back(line[i + 1]);
        ++i;
        continue;
      }
      // Trailing backslash: keep it.
      cur.push_back(c);
      continue;
    }
    cur.push_back(c);
  }
  flush();
  return out;
}

}  // namespace

std::array<uint8_t, 16> random_pack_id() {
  std::array<uint8_t, 16> id{};
  arc4random_buf(id.data(), id.size());
  return id;
}

std::string hex_pack_id(const std::array<uint8_t, 16>& id) {
  static const char* kHex = "0123456789abcdef";
  std::string out;
  out.reserve(32);
  for (uint8_t b : id) {
    out.push_back(kHex[(b >> 4) & 0xf]);
    out.push_back(kHex[b & 0xf]);
  }
  return out;
}

bool parse_hex_pack_id(std::string_view hex, std::array<uint8_t, 16>* out) {
  if (!out) return false;
  if (hex.size() != 32) return false;
  auto hexval = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
    if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
    return -1;
  };
  for (size_t i = 0; i < 16; ++i) {
    int hi = hexval(hex[2 * i]);
    int lo = hexval(hex[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    (*out)[i] = static_cast<uint8_t>((hi << 4) | lo);
  }
  return true;
}

bool encode_payload(const Pack& pack, std::vector<uint8_t>* out, std::string* error) {
  if (!out) return false;
  out->clear();
  if (pack.key_id.empty()) {
    if (error) *error = "missing key_id";
    return false;
  }
  if (pack.key_id.size() > 255u) {
    if (error) *error = "key_id too long";
    return false;
  }
  if (pack.steps.size() > kMaxPayloadSteps) {
    if (error) *error = "too many steps";
    return false;
  }
  size_t total_write = 0;
  for (const auto& step : pack.steps) {
    if (const auto* w = std::get_if<WriteFileStep>(&step)) {
      total_write += w->data.size();
      if (total_write > kMaxTotalWriteBytes) {
        if (error) *error = "total embedded write bytes too large";
        return false;
      }
    }
  }
  // Magic + version.
  out->push_back('A');
  out->push_back('P');
  out->push_back('K');
  out->push_back('1');
  write_u8(out, kPayloadVersion);
  write_u8(out, static_cast<uint8_t>(pack.key_id.size()));
  write_u16_le(out, 0);  // reserved
  write_u64_le(out, pack.created_ms);
  write_u64_le(out, pack.expires_ms);
  out->insert(out->end(), pack.pack_id.begin(), pack.pack_id.end());
  write_u32_le(out, static_cast<uint32_t>(pack.env.size()));
  write_u32_le(out, static_cast<uint32_t>(pack.steps.size()));
  out->insert(out->end(), pack.key_id.begin(), pack.key_id.end());

  // Env map.
  // (Order is not important because payload is what gets signed; the pack builder controls it.)
  for (const auto& kv : pack.env) {
    if (!write_bytes(out, kv.first, error)) return false;
    if (!write_bytes(out, kv.second, error)) return false;
  }

  // Steps.
  for (const auto& st : pack.steps) {
    if (const auto* step = std::get_if<ExecStep>(&st)) {
      // opcode 1 = exec
      write_u8(out, 1);
      write_u8(out, 0);     // flags (reserved)
      write_u16_le(out, 0); // reserved
      write_u32_le(out, step->timeout_ms);
      if (!write_bytes(out, step->cwd, error)) return false;
      if (step->argv.size() > 0xffffu) {
        if (error) *error = "too many argv entries";
        return false;
      }
      write_u16_le(out, static_cast<uint16_t>(step->argv.size()));
      for (const auto& arg : step->argv) {
        if (!write_bytes(out, arg, error)) return false;
      }
      continue;
    }
    if (const auto* w = std::get_if<WriteFileStep>(&st)) {
      // opcode 2 = write_file
      write_u8(out, 2);
      write_u8(out, 0);     // flags (reserved)
      write_u16_le(out, 0); // reserved
      write_u32_le(out, w->mode);
      if (!write_bytes(out, w->path, error)) return false;
      if (!write_blob(out, w->data, error)) return false;
      continue;
    }
    if (error) *error = "unknown step type";
    return false;
  }

  return true;
}

bool decode_payload(std::string_view payload, Pack* out, std::string* error) {
  if (!out) return false;
  out->steps.clear();
  out->env.clear();
  out->key_id.clear();

  Reader r;
  r.p = reinterpret_cast<const uint8_t*>(payload.data());
  r.n = payload.size();

  if (!r.need(4 + 1 + 1 + 2 + 8 + 8 + 16 + 4 + 4)) {
    if (error) *error = "payload too small";
    return false;
  }
  if (!(r.p[0] == 'A' && r.p[1] == 'P' && r.p[2] == 'K' && r.p[3] == '1')) {
    if (error) *error = "bad payload magic";
    return false;
  }
  r.skip(4);

  uint8_t version = 0;
  uint8_t key_id_len = 0;
  uint16_t reserved = 0;
  uint32_t env_count = 0;
  uint32_t step_count = 0;
  if (!r.read_u8(&version) || !r.read_u8(&key_id_len) || !r.read_u16_le(&reserved)) {
    if (error) *error = "payload header truncated";
    return false;
  }
  (void)reserved;
  if (version != 1 && version != 2) {
    if (error) *error = "unsupported payload version";
    return false;
  }
  if (!r.read_u64_le(&out->created_ms) || !r.read_u64_le(&out->expires_ms)) {
    if (error) *error = "payload header truncated";
    return false;
  }
  if (!r.need(16)) {
    if (error) *error = "payload header truncated";
    return false;
  }
  std::memcpy(out->pack_id.data(), r.p, 16);
  r.skip(16);
  if (!r.read_u32_le(&env_count) || !r.read_u32_le(&step_count)) {
    if (error) *error = "payload header truncated";
    return false;
  }
  if (!r.need(key_id_len)) {
    if (error) *error = "payload key_id truncated";
    return false;
  }
  out->key_id.assign(reinterpret_cast<const char*>(r.p), key_id_len);
  r.skip(key_id_len);

  // Env entries.
  for (uint32_t i = 0; i < env_count; ++i) {
    std::string k, v;
    if (!r.read_bytes(&k) || !r.read_bytes(&v)) {
      if (error) *error = "env truncated";
      return false;
    }
    out->env[std::move(k)] = std::move(v);
  }

  if (step_count > kMaxPayloadSteps) {
    if (error) *error = "too many steps";
    return false;
  }
  out->steps.reserve(step_count);
  size_t total_write = 0;
  for (uint32_t i = 0; i < step_count; ++i) {
    uint8_t opcode = 0, flags = 0;
    uint16_t res2 = 0;
    uint32_t timeout_ms = 0;
    std::string cwd;
    if (!r.read_u8(&opcode) || !r.read_u8(&flags) || !r.read_u16_le(&res2) ||
        !r.read_u32_le(&timeout_ms) || !r.read_bytes(&cwd)) {
      if (error) *error = "step truncated";
      return false;
    }
    (void)flags;
    (void)res2;
    if (opcode == 1) {
      uint16_t argc = 0;
      if (!r.read_u16_le(&argc)) {
        if (error) *error = "argv truncated";
        return false;
      }
      ExecStep step;
      step.timeout_ms = timeout_ms;
      step.cwd = std::move(cwd);
      step.argv.reserve(argc);
      for (uint16_t j = 0; j < argc; ++j) {
        std::string arg;
        if (!r.read_bytes(&arg)) {
          if (error) *error = "argv truncated";
          return false;
        }
        step.argv.emplace_back(std::move(arg));
      }
      out->steps.emplace_back(std::move(step));
      continue;
    }
    if (opcode == 2) {
      if (version == 1) {
        if (error) *error = "unsupported opcode";
        return false;
      }
      WriteFileStep w;
      w.mode = timeout_ms;
      w.path = std::move(cwd);
      uint32_t blob_len = 0;
      if (!r.read_u32_le(&blob_len)) {
        if (error) *error = "write truncated";
        return false;
      }
      if (total_write + blob_len > kMaxTotalWriteBytes) {
        if (error) *error = "total embedded write bytes too large";
        return false;
      }
      if (!r.need(blob_len)) {
        if (error) *error = "write truncated";
        return false;
      }
      w.data.assign(r.p, r.p + blob_len);
      r.skip(blob_len);
      total_write += w.data.size();
      out->steps.emplace_back(std::move(w));
      continue;
    }
    if (error) *error = "unsupported opcode";
    return false;
  }

  if (r.n != 0) {
    // Future-proofing: treat trailing bytes as an error for now.
    if (error) *error = "payload has trailing bytes";
    return false;
  }
  return true;
}

bool encode_envelope(const Envelope& env, std::vector<uint8_t>* out, std::string* error) {
  if (!out) return false;
  out->clear();
  if (env.payload.empty()) {
    if (error) *error = "empty payload";
    return false;
  }
  if (env.signature.empty()) {
    if (error) *error = "empty signature";
    return false;
  }
  if (env.payload.size() > 0xffffffffu || env.signature.size() > 0xffffffffu) {
    if (error) *error = "too large";
    return false;
  }
  out->push_back('S');
  out->push_back('A');
  out->push_back('P');
  out->push_back('1');
  write_u32_le(out, static_cast<uint32_t>(env.payload.size()));
  out->insert(out->end(), env.payload.begin(), env.payload.end());
  write_u32_le(out, static_cast<uint32_t>(env.signature.size()));
  out->insert(out->end(), env.signature.begin(), env.signature.end());
  return true;
}

bool decode_envelope(std::string_view bytes, Envelope* out, std::string* error) {
  if (!out) return false;
  out->payload.clear();
  out->signature.clear();
  Reader r;
  r.p = reinterpret_cast<const uint8_t*>(bytes.data());
  r.n = bytes.size();
  if (!r.need(4 + 4 + 4)) {
    if (error) *error = "envelope too small";
    return false;
  }
  if (!(r.p[0] == 'S' && r.p[1] == 'A' && r.p[2] == 'P' && r.p[3] == '1')) {
    if (error) *error = "bad envelope magic";
    return false;
  }
  r.skip(4);
  uint32_t payload_len = 0;
  if (!r.read_u32_le(&payload_len)) {
    if (error) *error = "envelope truncated";
    return false;
  }
  if (!r.need(payload_len + 4)) {
    if (error) *error = "envelope truncated";
    return false;
  }
  out->payload.assign(r.p, r.p + payload_len);
  r.skip(payload_len);
  uint32_t sig_len = 0;
  if (!r.read_u32_le(&sig_len)) {
    if (error) *error = "envelope truncated";
    return false;
  }
  if (!r.need(sig_len) || (r.n != sig_len)) {
    if (error) *error = "envelope truncated";
    return false;
  }
  out->signature.assign(r.p, r.p + sig_len);
  r.skip(sig_len);
  return true;
}

bool compile_script(std::string_view script,
                    const std::string& key_id,
                    uint64_t now_ms,
                    uint64_t ttl_ms,
                    Pack* out,
                    std::string* error) {
  if (!out) return false;
  out->steps.clear();
  out->env.clear();
  out->key_id = key_id;
  out->created_ms = now_ms;
  out->expires_ms = ttl_ms ? (now_ms + ttl_ms) : 0;
  out->pack_id = random_pack_id();

  std::string cwd;
  uint32_t timeout_ms = 0;
  size_t total_write = 0;

  size_t line_start = 0;
  while (line_start < script.size()) {
    size_t line_end = script.find('\n', line_start);
    if (line_end == std::string_view::npos) line_end = script.size();
    std::string_view line = script.substr(line_start, line_end - line_start);
    if (!line.empty() && line.back() == '\r') {
      line = line.substr(0, line.size() - 1);
    }
    line_start = line_end + 1;

    std::string trimmed = strings::trim(line);
    if (trimmed.empty()) continue;
    if (!trimmed.empty() && trimmed[0] == '#') continue;

    auto tokens = split_ws(trimmed);
    if (tokens.empty()) continue;

    const std::string& op = tokens[0];
    if (op == "cd") {
      if (tokens.size() != 2) {
        if (error) *error = "cd requires exactly 1 arg";
        return false;
      }
      cwd = tokens[1];
      continue;
    }
    if (op == "timeout") {
      if (tokens.size() != 2) {
        if (error) *error = "timeout requires exactly 1 arg";
        return false;
      }
      char* end = nullptr;
      errno = 0;
      unsigned long v = std::strtoul(tokens[1].c_str(), &end, 10);
      if (errno != 0 || !end || *end != '\0') {
        if (error) *error = "invalid timeout value";
        return false;
      }
      timeout_ms = static_cast<uint32_t>(std::min<unsigned long>(v, 0xfffffffful));
      continue;
    }
    if (op == "env") {
      if (tokens.size() != 2) {
        if (error) *error = "env requires exactly 1 arg (KEY=VALUE)";
        return false;
      }
      size_t eq = tokens[1].find('=');
      if (eq == std::string::npos || eq == 0) {
        if (error) *error = "env requires KEY=VALUE";
        return false;
      }
      std::string k = tokens[1].substr(0, eq);
      std::string v = tokens[1].substr(eq + 1);
      out->env[std::move(k)] = std::move(v);
      continue;
    }
    if (op == "put") {
      if (tokens.size() != 3) {
        if (error) *error = "put requires: put <dest_abs_path> @<src_path>";
        return false;
      }
      const std::string& dest = tokens[1];
      const std::string& src = tokens[2];
      if (dest.empty() || dest[0] != '/') {
        if (error) *error = "put destination must be an absolute path";
        return false;
      }
      if (src.size() < 2 || src[0] != '@') {
        if (error) *error = "put source must be @<path>";
        return false;
      }
      std::string src_path = src.substr(1);
      std::ifstream in(src_path, std::ios::binary);
      if (!in.good()) {
        if (error) *error = "put unable to open source: " + src_path;
        return false;
      }
      in.seekg(0, std::ios::end);
      std::streamoff size_off = in.tellg();
      if (size_off < 0) {
        if (error) *error = "put unable to stat source: " + src_path;
        return false;
      }
      size_t size = static_cast<size_t>(size_off);
      if (total_write + size > kMaxTotalWriteBytes) {
        if (error) *error = "total embedded write bytes too large";
        return false;
      }
      in.seekg(0, std::ios::beg);
      std::vector<uint8_t> data;
      data.resize(size);
      if (size > 0) {
        in.read(reinterpret_cast<char*>(data.data()), static_cast<std::streamsize>(size));
        if (!in.good()) {
          if (error) *error = "put read failed: " + src_path;
          return false;
        }
      }
      total_write += data.size();
      WriteFileStep w;
      w.path = dest;
      w.data = std::move(data);
      w.mode = 0644;
      out->steps.emplace_back(std::move(w));
      continue;
    }
    if (op == "exec") {
      if (tokens.size() < 2) {
        if (error) *error = "exec requires at least 1 arg";
        return false;
      }
      ExecStep step;
      step.cwd = cwd;
      step.timeout_ms = timeout_ms;
      step.argv.assign(tokens.begin() + 1, tokens.end());
      out->steps.emplace_back(std::move(step));
      continue;
    }

    if (error) *error = "unknown instruction: " + op;
    return false;
  }

  if (out->steps.empty()) {
    if (error) *error = "script has no steps";
    return false;
  }
  return true;
}

}  // namespace action_pack
