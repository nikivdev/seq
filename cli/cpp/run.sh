#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/out"
LOG="$OUT/logs"
BIN="$OUT/bin"
BUILD="$OUT/build"

mkdir -p "$LOG" "$BIN" "$BUILD"

# Regenerate seq macros from the Karabiner config (fast; keeps seqd macros in sync).
# This keeps `seq run "<macro>"` on the fast path without temp inline YAML fallbacks.
if [ -x "$(command -v python3)" ] && [ -f "$ROOT/../../tools/gen_macros.py" ]; then
  python3 "$ROOT/../../tools/gen_macros.py" 2>&1 | tee "$LOG/gen_macros.log" >/dev/null || true
fi

# Convention:
# - `./run.sh build` builds only (no execution)
# - `./run.sh deploy` builds + restarts the daemon if Flow is available
# - `./run.sh test` runs perf-smoke tests (builds first)
# - `./run.sh` defaults to showing help
MODE="${1:-run}"
if [ "$MODE" = "build" ] || [ "$MODE" = "deploy" ] || [ "$MODE" = "test" ]; then
  shift
fi
if [ "$MODE" = "run" ] && [ $# -eq 0 ]; then
  set -- help
fi

# Build the in-process Swift memory engine (Wax-backed). Stage in $BUILD and atomically
# move into $BIN to avoid modifying a dylib in-place while a daemon is running.
if [ -x "$ROOT/../swift/seqmem/run.sh" ]; then
  "$ROOT/../swift/seqmem/run.sh" build --copy-to "$BUILD" 2>&1 | tee "$LOG/seqmem.build.log"
fi

CXX="${CXX:-clang++}"
if ! command -v "$CXX" >/dev/null 2>&1; then
  CXX="g++"
fi

# Zero-cost optimized build flags (enforced by: rise optimize cli cpp)
CXXFLAGS="${CXXFLAGS:--O3 -g -std=c++2b -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion}"
# Additional optimizations for release builds
CXXFLAGS="$CXXFLAGS -march=native -mtune=native -flto"
# Taskflow (optional; used for parallel step execution)
TF_DIR="${HOME}/repos/taskflow/taskflow"
if [ -f "$TF_DIR/taskflow/taskflow.hpp" ]; then
  # Homebrew clang often pairs its libc++ headers with the system libc++ dylib,
  # which breaks Taskflow linking on macOS (missing std::__1::__hash_memory).
  # Prefer Apple clang when Taskflow is present.
  if [ "$CXX" = "clang++" ] && [ -x "/usr/bin/clang++" ]; then
    CXX="/usr/bin/clang++"
  fi
  # Treat Taskflow as a system include to avoid noisy third-party warnings.
  CXXFLAGS="$CXXFLAGS -isystem $TF_DIR"
fi
# Optional: disable RTTI/exceptions if not used
# CXXFLAGS="$CXXFLAGS -fno-rtti -fno-exceptions"
# Build ClickHouse native-protocol wrapper (libseqch.dylib) via CMake + FetchContent.
# Skipped if deps/CMakeLists.txt is missing or cmake is not available.
CH_BUILD="$BUILD/ch"
CH_FLAGS=""
CH_LINK=""
if [ -f "$ROOT/deps/CMakeLists.txt" ] && command -v cmake >/dev/null 2>&1; then
  if [ ! -f "$CH_BUILD/libseqch.dylib" ]; then
    echo "building clickhouse-cpp + seqch wrapper..." >&2
    OSX_TARGET="${SEQ_OSX_DEPLOYMENT_TARGET:-$(/usr/bin/sw_vers -productVersion | awk -F. '{print $1\".\"$2}')}"
    cmake -S "$ROOT/deps" -B "$CH_BUILD" \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
      -DCMAKE_OSX_DEPLOYMENT_TARGET="$OSX_TARGET" \
      -DCMAKE_CXX_STANDARD=20 \
      2>&1 | tee "$LOG/ch_cmake.log"
    cmake --build "$CH_BUILD" --config Release -- -j"$(sysctl -n hw.ncpu)" 2>&1 | tee "$LOG/ch_build.log"
  fi
  CH_LIB="$(find "$CH_BUILD" -maxdepth 4 -name 'libseqch.dylib' -print -quit 2>/dev/null || true)"
  if [ -n "${CH_LIB}" ] && [ -f "${CH_LIB}" ]; then
    cp -f "${CH_LIB}" "$BUILD/libseqch.dylib"
    CH_FLAGS="-DSEQ_HAS_CLICKHOUSE=1"
    CH_LINK="-L$BUILD -lseqch -Wl,-rpath,@executable_path"
  else
    echo "note: clickhouse wrapper not built (libseqch.dylib not found under $CH_BUILD)" >&2
  fi
fi

# Exclude clickhouse wrapper sources (built by CMake above, not by direct clang++ invocation).
SRC_CPP=$(find "$ROOT/src" -name '*.cpp' ! -name 'clickhouse.cpp' ! -name 'clickhouse_bridge.cpp' -print)
SRC_MM=$(find "$ROOT/src" -name '*.mm' -print)
SRC="$SRC_CPP $SRC_MM"
# ObjC++ files need ARC for correct block capture of NSString* in async callbacks.
CXXFLAGS="$CXXFLAGS -fobjc-arc $CH_FLAGS"

if [ -z "$SRC" ]; then
  echo "no .cpp files in $ROOT/src"
  exit 1
fi

"$CXX" $CXXFLAGS $SRC \
	  -framework Cocoa -framework ApplicationServices \
	  -framework ScreenCaptureKit -framework Vision \
	  -framework CoreImage -framework CoreMedia -framework CoreVideo -framework ImageIO \
	  -framework Security \
	  -lsqlite3 \
	  $CH_LINK \
	  -o "$BUILD/seq" 2>&1 | tee "$LOG/compile.log"

# Codesign with Developer ID Application to make TCC (Accessibility/Input Monitoring) grants
# sticky across rebuilds. Falls back to Apple Development if no Developer ID is available.
if [ -n "${SEQ_CODE_SIGN_IDENTITY:-}" ]; then
  export FLOW_CODESIGN_IDENTITY="$SEQ_CODE_SIGN_IDENTITY"
fi
source "${HOME}/.config/flow/codesign.sh" 2>/dev/null || true
IDENTITY="${FLOW_CODESIGN_IDENTITY:-}"
flow_codesign "$BUILD/seq" 2>/dev/null || true
if [ -f "$BUILD/libseqmem.dylib" ]; then
  flow_codesign "$BUILD/libseqmem.dylib" 2>/dev/null || true
fi
if [ -f "$BUILD/libseqch.dylib" ]; then
  flow_codesign "$BUILD/libseqch.dylib" 2>/dev/null || true
fi

# Atomically publish artifacts. This avoids killing a running hardened-runtime
# process due to in-place writes to an already executing binary/dylib.
mv -f "$BUILD/seq" "$BIN/seq"
chmod +x "$BIN/seq"
ln -sf "$BIN/seq" "$BIN/seqd"
if [ -f "$BUILD/libseqmem.dylib" ]; then
  mv -f "$BUILD/libseqmem.dylib" "$BIN/libseqmem.dylib"
  chmod +x "$BIN/libseqmem.dylib" || true
fi
if [ -f "$BUILD/libseqch.dylib" ]; then
  mv -f "$BUILD/libseqch.dylib" "$BIN/libseqch.dylib"
  chmod +x "$BIN/libseqch.dylib" || true
fi

# Build a minimal .app bundle for the daemon so macOS TCC grants Accessibility
# to the daemon process (bare CLI tools only inherit TCC from their terminal).
APP="$BIN/SeqDaemon.app"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN/seq" "$APP/Contents/MacOS/seqd"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>dev.nikiv.seqd</string>
  <key>CFBundleName</key>
  <string>SeqDaemon</string>
  <key>CFBundleExecutable</key>
  <string>seqd</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSScreenCaptureUsageDescription</key>
  <string>SeqDaemon captures screen frames for context search and recall.</string>
</dict>
</plist>
PLIST
if [ -n "$IDENTITY" ]; then
  /usr/bin/codesign --force --options runtime --sign "$IDENTITY" "$APP" >/dev/null 2>&1 || true
fi

if [ "$MODE" = "build" ]; then
  exit 0
fi
if [ "$MODE" = "deploy" ]; then
  # Best-effort: if Flow is installed and seqd is configured, restart it.
  if command -v f >/dev/null 2>&1 && [ -f "$ROOT/../../flow.toml" ]; then
    (cd "$ROOT/../.." && f daemon restart seqd) || true
  fi
  exit 0
fi
if [ "$MODE" = "test" ]; then
  "$ROOT/../../tools/perf_smoke_test.sh"
  exit 0
fi

RISE_LOG_DIR="$LOG" "$BIN/seq" "$@"
