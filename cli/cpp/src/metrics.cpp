#include "metrics.h"

#include "base.h"

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <string_view>

#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <unistd.h>

namespace metrics {
namespace {

using fn_configure_wax_path = void (*)(const char*, int);
using fn_record_request = void (*)(const uint8_t*,
                                   int,
                                   uint64_t,
                                   uint64_t,
                                   uint8_t,
                                   const uint8_t*,
                                   int);
using fn_metrics_json = char* (*)();
using fn_tail_json = char* (*)(int32_t);
using fn_free = void (*)(void*);

struct Api {
  void* handle = nullptr;
  fn_configure_wax_path configure_wax_path = nullptr;
  fn_record_request record_request = nullptr;
  fn_metrics_json metrics_json = nullptr;
  fn_tail_json tail_json = nullptr;
  fn_free free = nullptr;
  bool ok = false;
};

Api& api() {
  static Api a;
  return a;
}

std::once_flag& init_once_flag() {
  static std::once_flag f;
  return f;
}

std::string executable_dir() {
  uint32_t size = 0;
  _NSGetExecutablePath(nullptr, &size);
  if (size == 0 || size > 1 << 20) {
    return {};
  }
  std::string buf(size, '\0');
  if (_NSGetExecutablePath(buf.data(), &size) != 0) {
    return {};
  }
  // _NSGetExecutablePath may not null-terminate if truncated; ensure it.
  buf.resize(std::strlen(buf.c_str()));

  char realbuf[PATH_MAX];
  if (!::realpath(buf.c_str(), realbuf)) {
    std::strncpy(realbuf, buf.c_str(), sizeof(realbuf));
    realbuf[sizeof(realbuf) - 1] = '\0';
  }

  std::string path(realbuf);
  size_t slash = path.rfind('/');
  if (slash == std::string::npos) {
    return {};
  }
  return path.substr(0, slash);
}

std::string default_dylib_path() {
  std::string dir = executable_dir();
  if (dir.empty()) {
    return {};
  }
  return dir + "/libseqmem.dylib";
}

const char* dylib_path() {
  const char* env = std::getenv("SEQ_MEM_DYLIB_PATH");
  if (env && *env) {
    return env;
  }
  static std::string path = default_dylib_path();
  return path.empty() ? nullptr : path.c_str();
}

void init_api_once() {
  Api& a = api();
  const char* path = dylib_path();
  if (!path || !*path) {
    a.ok = false;
    return;
  }

  void* h = ::dlopen(path, RTLD_NOW | RTLD_LOCAL);
  if (!h) {
    a.ok = false;
    return;
  }

  a.handle = h;
  a.configure_wax_path =
      reinterpret_cast<fn_configure_wax_path>(::dlsym(h, "seqmem_configure_wax_path"));
  a.record_request =
      reinterpret_cast<fn_record_request>(::dlsym(h, "seqmem_record_request"));
  a.metrics_json =
      reinterpret_cast<fn_metrics_json>(::dlsym(h, "seqmem_metrics_json"));
  a.tail_json =
      reinterpret_cast<fn_tail_json>(::dlsym(h, "seqmem_tail_json"));
  a.free = reinterpret_cast<fn_free>(::dlsym(h, "seqmem_free"));

  a.ok = a.record_request && a.metrics_json && a.tail_json && a.free;
  if (!a.ok) {
    return;
  }

  const char* wax = std::getenv("SEQ_MEM_WAX_PATH");
  if (wax && *wax && a.configure_wax_path) {
    a.configure_wax_path(wax, static_cast<int>(std::strlen(wax)));
  }
}

ALWAYS_INLINE bool ensure_ready() {
  std::call_once(init_once_flag(), init_api_once);
  return api().ok;
}

}  // namespace

void record(std::string_view name,
            uint64_t ts_ms,
            uint64_t dur_us,
            bool ok,
            std::string_view subject) {
  if (!ensure_ready()) {
    return;
  }
  Api& a = api();
  if (unlikely(!a.record_request)) {
    return;
  }

  const uint8_t* name_ptr = reinterpret_cast<const uint8_t*>(name.data());
  int name_len = static_cast<int>(name.size());
  const uint8_t* subj_ptr =
      subject.empty() ? nullptr : reinterpret_cast<const uint8_t*>(subject.data());
  int subj_len = static_cast<int>(subject.size());

  // Best-effort only: never throw, never block.
  a.record_request(name_ptr,
                   name_len,
                   ts_ms,
                   dur_us,
                   ok ? 1 : 0,
                   subj_ptr,
                   subj_len);
}

std::string metrics_json() {
  if (!ensure_ready()) {
    return "{\"error\":\"seqmem_unavailable\"}";
  }
  Api& a = api();
  if (unlikely(!a.metrics_json || !a.free)) {
    return "{\"error\":\"seqmem_unavailable\"}";
  }
  char* p = a.metrics_json();
  if (!p) {
    return "{\"error\":\"seqmem_null\"}";
  }
  std::string out(p);
  a.free(p);
  return out;
}

std::string tail_json(int max_events) {
  if (!ensure_ready()) {
    return "{\"error\":\"seqmem_unavailable\"}";
  }
  Api& a = api();
  if (unlikely(!a.tail_json || !a.free)) {
    return "{\"error\":\"seqmem_unavailable\"}";
  }
  if (max_events < 0) {
    max_events = 0;
  }
  char* p = a.tail_json(static_cast<int32_t>(max_events));
  if (!p) {
    return "{\"error\":\"seqmem_null\"}";
  }
  std::string out(p);
  a.free(p);
  return out;
}

}  // namespace metrics

