#pragma once

#include <string>

namespace capture {

struct Config {
  std::string spool_dir;    // local staging for HEIC frames
  std::string fts_db_path;  // SQLite FTS5 database path
  std::string sync_script;  // path to sync_frames.sh
  int max_spool_mb  = 200;  // delete oldest when exceeded
  int hash_threshold = 8;   // perceptual hash Hamming distance
};

// Start screen capture pipeline (ScreenCaptureKit + Vision OCR + FTS5).
// Frames go to spool_dir, OCR text to fts_db_path.
// Best-effort: silently no-ops if Screen Recording permission is missing.
void start(const Config& cfg);

void stop();

// FTS5 search across OCR text. Returns JSON.
std::string search(const std::string& query, int max_results);

}  // namespace capture
