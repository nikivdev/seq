#pragma once

#include <string>
#include <string_view>

namespace strings {
std::string trim(std::string_view value);
std::string strip_quotes(std::string_view value);
bool starts_with(std::string_view value, std::string_view prefix);
}  // namespace strings
