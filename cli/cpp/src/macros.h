#pragma once

#include "base.h"

#include <string>
#include <string_view>
#include <vector>

namespace macros {
enum class ActionType {
  OpenApp,
  OpenAppToggle,
  OpenUrl,
  SessionSave,
  PasteText,
  SwitchWindowOrApp,
  Keystroke,
  SelectMenuItem,
  Click,
  DoubleClick,
  RightClick,
  Scroll,
  Drag,
  MouseMove,
  Screenshot,
  Sequence,
  Todo,
  Unknown,
};

struct Step {
  ActionType action = ActionType::Unknown;
  std::string arg;
  std::string app;
  // If true, this step is eligible to run concurrently with adjacent
  // `parallel: true` steps (subject to safety checks in the executor).
  bool parallel = false;
};

struct Macro {
  std::string name;
  ActionType action = ActionType::Unknown;
  std::string arg;
  std::string app;
  std::vector<Step> steps;
};

using Registry = std::vector<Macro>;

bool load(const std::string& path, Registry* out, std::string* error);
bool load_append(const std::string& path, Registry* inout, std::string* error);

// O(n) linear search - consider hash table for large registries
ALWAYS_INLINE const Macro* find(const Registry& registry, std::string_view name) {
  for (const auto& entry : registry) {
    if (likely(entry.name == name)) {
      return &entry;
    }
  }
  return nullptr;
}

std::string action_to_string(ActionType action);
ActionType parse_action(std::string_view value);
}  // namespace macros
