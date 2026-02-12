#include "base64.h"

#include <array>

namespace base64 {
namespace {
constexpr char kAlphabet[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::array<int8_t, 256> make_dec_table() {
  std::array<int8_t, 256> t{};
  t.fill(-1);
  for (int i = 0; i < 64; ++i) {
    t[static_cast<uint8_t>(kAlphabet[i])] = static_cast<int8_t>(i);
  }
  t[static_cast<uint8_t>('=')] = -2;  // padding
  return t;
}

const std::array<int8_t, 256>& dec_table() {
  static const std::array<int8_t, 256> t = make_dec_table();
  return t;
}

inline bool is_ws(char c) {
  return c == ' ' || c == '\n' || c == '\r' || c == '\t';
}
}  // namespace

std::string encode(const uint8_t* data, size_t len) {
  if (!data || len == 0) {
    return {};
  }
  std::string out;
  out.reserve(((len + 2) / 3) * 4);
  size_t i = 0;
  while (i + 3 <= len) {
    uint32_t v = (static_cast<uint32_t>(data[i]) << 16) |
                 (static_cast<uint32_t>(data[i + 1]) << 8) |
                 (static_cast<uint32_t>(data[i + 2]));
    out.push_back(kAlphabet[(v >> 18) & 0x3f]);
    out.push_back(kAlphabet[(v >> 12) & 0x3f]);
    out.push_back(kAlphabet[(v >> 6) & 0x3f]);
    out.push_back(kAlphabet[v & 0x3f]);
    i += 3;
  }
  size_t rem = len - i;
  if (rem == 1) {
    uint32_t v = (static_cast<uint32_t>(data[i]) << 16);
    out.push_back(kAlphabet[(v >> 18) & 0x3f]);
    out.push_back(kAlphabet[(v >> 12) & 0x3f]);
    out.push_back('=');
    out.push_back('=');
  } else if (rem == 2) {
    uint32_t v = (static_cast<uint32_t>(data[i]) << 16) |
                 (static_cast<uint32_t>(data[i + 1]) << 8);
    out.push_back(kAlphabet[(v >> 18) & 0x3f]);
    out.push_back(kAlphabet[(v >> 12) & 0x3f]);
    out.push_back(kAlphabet[(v >> 6) & 0x3f]);
    out.push_back('=');
  }
  return out;
}

bool decode(std::string_view b64, std::vector<uint8_t>* out) {
  if (!out) return false;
  out->clear();

  const auto& tab = dec_table();
  // Upper bound (ignoring whitespace).
  out->reserve((b64.size() / 4) * 3);

  uint32_t acc = 0;
  int acc_bits = 0;
  int pad = 0;

  auto push_byte = [&](uint8_t byte) {
    out->push_back(byte);
  };

  for (char c : b64) {
    if (is_ws(c)) continue;
    int8_t v = tab[static_cast<uint8_t>(c)];
    if (v == -1) {
      return false;
    }
    if (v == -2) {  // '='
      ++pad;
      // Treat padding as zero bits, but track it.
      v = 0;
    } else if (pad != 0) {
      // Non-padding after padding is invalid.
      return false;
    }

    acc = (acc << 6) | static_cast<uint32_t>(v);
    acc_bits += 6;
    if (acc_bits >= 8) {
      acc_bits -= 8;
      uint8_t byte = static_cast<uint8_t>((acc >> acc_bits) & 0xff);
      push_byte(byte);
    }
  }

  // base64 input should end on a 4-char boundary (ignoring whitespace).
  // Our bit accumulator method may have emitted extra bytes for padding; trim.
  if (pad > 0) {
    if (pad == 1) {
      if (out->empty()) return false;
      out->pop_back();
    } else if (pad == 2) {
      if (out->size() < 2) return false;
      out->pop_back();
      out->pop_back();
    } else {
      return false;
    }
  }

  return true;
}
}  // namespace base64

