#pragma once

#include "macros.h"

#include <string>

namespace actions {
struct Result {
  bool ok = false;
  std::string error;
};

struct FrontmostApp {
  std::string name;
  std::string bundle_id;
  std::string bundle_url;
  pid_t pid = 0;
};

Result run(const macros::Macro& macro);
Result open_app(std::string_view app);
Result open_app_toggle(std::string_view app);
// Open a file/folder path with a specific application (no fork+exec).
Result open_with_app(std::string_view app_path, std::string_view file_path);
std::string frontmost_app_name();
FrontmostApp frontmost_app();
// JSON array of running apps with name/bundle_id/pid/bundle_url.
std::string running_apps_json();
Result switch_window_or_app();
// Select a menu item by path (e.g. "Spaces\t21") in a specific app.
Result select_menu_item(std::string_view app, std::string_view path);
Result mouse_click(double x, double y);
Result mouse_double_click(double x, double y);
Result mouse_right_click(double x, double y);
Result mouse_scroll(double x, double y, int dy);
Result mouse_drag(double x1, double y1, double x2, double y2);
Result mouse_move(double x, double y);
Result screenshot(const std::string& path);
// Pre-warm the app cache with bundle IDs of all currently running apps.
void prewarm_app_cache();
}  // namespace actions
