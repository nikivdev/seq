#pragma once

#include "base.h"

#include <cstddef>
#include <string_view>

namespace io {

// Buffered output writer - reduces syscall overhead for small writes
// Buffer size: 4KB (one page)
class Out {
 public:
  explicit Out(int fd);
  ~Out();

  void write(std::string_view sv);
  void write(char c);  // ALWAYS_INLINE in .cpp
  void flush();

 private:
  int fd_;
  char buf_[4096];
  size_t used_;
};

extern Out out;
extern Out err;
}  // namespace io
