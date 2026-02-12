#include "strings.h"

namespace strings {
std::string trim(std::string_view value) {
  while (!value.empty()) {
    char c = value.front();
    if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
      value.remove_prefix(1);
      continue;
    }
    break;
  }
  while (!value.empty()) {
    char c = value.back();
    if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
      value.remove_suffix(1);
      continue;
    }
    break;
  }
  return std::string(value);
}

std::string strip_quotes(std::string_view value) {
  std::string trimmed = trim(value);
  std::string_view view(trimmed);
  if (view.size() >= 2) {
    char first = view.front();
    char last = view.back();
    if ((first == '"' && last == '"') || (first == '\'' && last == '\'')) {
      view.remove_prefix(1);
      view.remove_suffix(1);
    }
  }
  return std::string(view);
}

bool starts_with(std::string_view value, std::string_view prefix) {
  if (prefix.size() > value.size()) {
    return false;
  }
  return value.compare(0, prefix.size(), prefix) == 0;
}
}  // namespace strings
