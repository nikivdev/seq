#include "macros.h"

#include "base.h"
#include "strings.h"

#include <fstream>
#include <optional>
#include <sstream>
#include <string_view>

namespace macros {
namespace {
bool starts_with(std::string_view value, std::string_view prefix) {
  return strings::starts_with(value, prefix);
}

std::string_view trim_prefix(std::string_view value, std::string_view prefix) {
  if (!starts_with(value, prefix)) {
    return value;
  }
  value.remove_prefix(prefix.size());
  return value;
}
}  // namespace

ActionType parse_action(std::string_view value) {
  std::string v = strings::trim(value);
  for (auto& c : v) {
    if (c >= 'A' && c <= 'Z') {
      c = static_cast<char>(c - 'A' + 'a');
    }
  }
  if (v == "open_app") {
    return ActionType::OpenApp;
  }
  if (v == "open_app_toggle") {
    return ActionType::OpenAppToggle;
  }
  if (v == "open_url") {
    return ActionType::OpenUrl;
  }
  if (v == "session_save") {
    return ActionType::SessionSave;
  }
  if (v == "paste_text") {
    return ActionType::PasteText;
  }
  if (v == "run_script" || v == "script") {
    return ActionType::RunScript;
  }
  if (v == "switch_window_or_app") {
    return ActionType::SwitchWindowOrApp;
  }
  if (v == "keystroke") {
    return ActionType::Keystroke;
  }
  if (v == "select_menu_item" || v == "menu_item" || v == "menu") {
    return ActionType::SelectMenuItem;
  }
  if (v == "click") {
    return ActionType::Click;
  }
  if (v == "double_click") {
    return ActionType::DoubleClick;
  }
  if (v == "right_click") {
    return ActionType::RightClick;
  }
  if (v == "scroll") {
    return ActionType::Scroll;
  }
  if (v == "drag") {
    return ActionType::Drag;
  }
  if (v == "mouse_move") {
    return ActionType::MouseMove;
  }
  if (v == "screenshot") {
    return ActionType::Screenshot;
  }
  if (v == "sequence") {
    return ActionType::Sequence;
  }
  if (v == "todo") {
    return ActionType::Todo;
  }
  return ActionType::Unknown;
}

std::optional<bool> parse_bool(std::string_view value) {
  std::string v = strings::trim(value);
  for (auto& c : v) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  if (v == "true" || v == "1" || v == "yes" || v == "y") return true;
  if (v == "false" || v == "0" || v == "no" || v == "n") return false;
  return std::nullopt;
}

std::string action_to_string(ActionType action) {
  switch (action) {
    case ActionType::OpenApp:
      return "open_app";
    case ActionType::OpenAppToggle:
      return "open_app_toggle";
    case ActionType::OpenUrl:
      return "open_url";
    case ActionType::SessionSave:
      return "session_save";
    case ActionType::PasteText:
      return "paste_text";
    case ActionType::RunScript:
      return "run_script";
    case ActionType::SwitchWindowOrApp:
      return "switch_window_or_app";
    case ActionType::Keystroke:
      return "keystroke";
    case ActionType::SelectMenuItem:
      return "select_menu_item";
    case ActionType::Click:
      return "click";
    case ActionType::DoubleClick:
      return "double_click";
    case ActionType::RightClick:
      return "right_click";
    case ActionType::Scroll:
      return "scroll";
    case ActionType::Drag:
      return "drag";
    case ActionType::MouseMove:
      return "mouse_move";
    case ActionType::Screenshot:
      return "screenshot";
    case ActionType::Sequence:
      return "sequence";
    case ActionType::Todo:
      return "todo";
    case ActionType::Unknown:
    default:
      return "unknown";
  }
}

bool load(const std::string& path, Registry* out, std::string* error) {
  std::ifstream in(path);
  if (!in) {
    if (error) {
      *error = "failed to open macros file: " + path;
    }
    return false;
  }

  Registry registry;
  Macro current;
  bool in_steps = false;
  Step current_step;
  std::string line;
  auto flush_step = [&]() {
    if (current_step.action != ActionType::Unknown ||
        !current_step.arg.empty() ||
        !current_step.app.empty()) {
      current.steps.push_back(current_step);
      current_step = Step{};
    }
  };
  auto flush = [&]() {
    flush_step();
    in_steps = false;
    if (!current.name.empty()) {
      registry.push_back(current);
      current = Macro{};
    }
  };

  while (std::getline(in, line)) {
    std::string trimmed = strings::trim(line);
    if (trimmed.empty() || trimmed[0] == '#') {
      continue;
    }
    if (starts_with(trimmed, "- name:")) {
      flush();
      std::string_view value = trim_prefix(trimmed, "- name:");
      current.name = strings::strip_quotes(value);
      continue;
    }
    if (trimmed == "steps:" || starts_with(trimmed, "steps:")) {
      in_steps = true;
      continue;
    }
    if (in_steps && starts_with(trimmed, "- action:")) {
      flush_step();
      std::string_view value = trim_prefix(trimmed, "- action:");
      current_step.action = parse_action(strings::strip_quotes(value));
      continue;
    }
    if (starts_with(trimmed, "action:")) {
      std::string_view value = trim_prefix(trimmed, "action:");
      current.action = parse_action(strings::strip_quotes(value));
      continue;
    }
    if (starts_with(trimmed, "arg:")) {
      std::string_view value = trim_prefix(trimmed, "arg:");
      if (in_steps) {
        current_step.arg = strings::strip_quotes(value);
      } else {
        current.arg = strings::strip_quotes(value);
      }
      continue;
    }
    if (in_steps && starts_with(trimmed, "parallel:")) {
      std::string_view value = trim_prefix(trimmed, "parallel:");
      auto b = parse_bool(strings::strip_quotes(value));
      if (b) {
        current_step.parallel = *b;
      }
      continue;
    }
    if (starts_with(trimmed, "app:")) {
      std::string_view value = trim_prefix(trimmed, "app:");
      if (in_steps) {
        current_step.app = strings::strip_quotes(value);
      } else {
        current.app = strings::strip_quotes(value);
      }
      continue;
    }
  }
  flush();

  *out = std::move(registry);
  return true;
}

bool load_append(const std::string& path, Registry* inout, std::string* error) {
  Registry tmp;
  if (!load(path, &tmp, error)) {
    return false;
  }
  // Overlay semantics: entries in the appended file override earlier entries with the same name.
  for (auto& m : tmp) {
    bool replaced = false;
    for (auto& existing : *inout) {
      if (existing.name == m.name) {
        existing = std::move(m);
        replaced = true;
        break;
      }
    }
    if (!replaced) {
      inout->push_back(std::move(m));
    }
  }
  return true;
}

}  // namespace macros
