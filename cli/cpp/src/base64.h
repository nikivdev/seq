#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace base64 {

// Standard base64 (RFC 4648) with '=' padding.
std::string encode(const uint8_t* data, size_t len);
inline std::string encode(const std::vector<uint8_t>& data) {
  return encode(data.data(), data.size());
}

// Decodes standard base64. Returns false on invalid input.
bool decode(std::string_view b64, std::vector<uint8_t>* out);

}  // namespace base64

