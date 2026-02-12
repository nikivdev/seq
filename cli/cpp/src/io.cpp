#include "io.h"

#include "base.h"

#include <cstring>
#include <errno.h>
#include <unistd.h>

namespace io {
namespace {
NO_INLINE void write_all(int fd, const char* RESTRICT data, size_t len) {
  size_t offset = 0;
  while (likely(offset < len)) {
    ssize_t n = ::write(fd, data + offset, len - offset);
    if (likely(n > 0)) {
      offset += static_cast<size_t>(n);
      continue;
    }
    if (n < 0 && errno == EINTR) {
      continue;
    }
    break;
  }
}
}  // namespace

Out::Out(int fd) : fd_(fd), used_(0) {}

Out::~Out() {
  flush();
}

void Out::flush() {
  if (unlikely(used_ == 0)) {
    return;
  }
  write_all(fd_, buf_, used_);
  used_ = 0;
}

void Out::write(std::string_view sv) {
  if (unlikely(sv.empty())) {
    return;
  }

  // Large writes bypass buffer
  if (unlikely(sv.size() > sizeof(buf_))) {
    flush();
    write_all(fd_, sv.data(), sv.size());
    return;
  }

  // Flush if buffer would overflow
  if (unlikely(used_ + sv.size() > sizeof(buf_))) {
    flush();
  }

  std::memcpy(buf_ + used_, sv.data(), sv.size());
  used_ += sv.size();
}

ALWAYS_INLINE void Out::write(char c) {
  if (unlikely(used_ == sizeof(buf_))) {
    flush();
  }
  buf_[used_++] = c;
}

Out out(1);
Out err(2);
}  // namespace io
