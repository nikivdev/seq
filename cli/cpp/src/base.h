#pragma once

#include <cstddef>

// Zero-cost C++ abstractions inspired by ClickHouse
// Apply with: rise optimize cli cpp

// ============================================================================
// Branch Prediction Hints
// ============================================================================
// Use in hot paths where branch outcome is predictable
// Example: if (likely(ptr != nullptr)) { ... }

#if defined(__GNUC__) || defined(__clang__)
#define likely(x) __builtin_expect(!!(x), 1)
#define unlikely(x) __builtin_expect(!!(x), 0)
#else
#define likely(x) (x)
#define unlikely(x) (x)
#endif

// ============================================================================
// Inline Control
// ============================================================================
// ALWAYS_INLINE: Force inline for hot paths (fast path code)
// NO_INLINE: Prevent inline for cold paths (error handling, rare branches)
// Pattern: inline fast path, outline slow path

#if defined(__GNUC__) || defined(__clang__)
#define ALWAYS_INLINE __attribute__((__always_inline__)) inline
#define NO_INLINE __attribute__((__noinline__))
#define FLATTEN __attribute__((__flatten__))
#else
#define ALWAYS_INLINE inline
#define NO_INLINE
#define FLATTEN
#endif

// ============================================================================
// Type Aliasing
// ============================================================================
// MAY_ALIAS: Allow type punning through this type
// Use for SIMD intrinsics and low-level memory operations

#if defined(__GNUC__) || defined(__clang__)
#define MAY_ALIAS __attribute__((__may_alias__))
#else
#define MAY_ALIAS
#endif

// ============================================================================
// Restrict Pointers
// ============================================================================
// RESTRICT: Promise no aliasing for better optimization
// Example: void copy(char* RESTRICT dst, const char* RESTRICT src, size_t n)

#if defined(__GNUC__) || defined(__clang__)
#define RESTRICT __restrict__
#elif defined(_MSC_VER)
#define RESTRICT __restrict
#else
#define RESTRICT
#endif

// ============================================================================
// Prefetch Hints
// ============================================================================
// Prefetch data into cache before access
// Locality: 0 = no temporal locality (use once), 3 = high temporal locality

#if defined(__GNUC__) || defined(__clang__)
#define PREFETCH(addr) __builtin_prefetch(addr)
#define PREFETCH_L1(addr) __builtin_prefetch(addr, 0, 3)
#define PREFETCH_L2(addr) __builtin_prefetch(addr, 0, 2)
#define PREFETCH_NT(addr) __builtin_prefetch(addr, 0, 0)
#else
#define PREFETCH(addr)
#define PREFETCH_L1(addr)
#define PREFETCH_L2(addr)
#define PREFETCH_NT(addr)
#endif

// ============================================================================
// Cache Line Alignment
// ============================================================================
// Align hot data to cache line boundaries to avoid false sharing

#define CACHE_LINE_SIZE 64
#define CACHE_ALIGNED alignas(CACHE_LINE_SIZE)

// ============================================================================
// SIMD Constants
// ============================================================================
// Padding for safe SIMD reads beyond array bounds

#define SIMD_PADDING 16  // SSE: 16 bytes, AVX: 32, AVX-512: 64

// ============================================================================
// Unreachable Hint
// ============================================================================
// Mark code paths that should never execute

#if defined(__GNUC__) || defined(__clang__)
#define UNREACHABLE() __builtin_unreachable()
#elif defined(_MSC_VER)
#define UNREACHABLE() __assume(0)
#else
#define UNREACHABLE()
#endif

// ============================================================================
// Assume Hint
// ============================================================================
// Tell compiler a condition is always true for optimization

#if defined(__clang__)
#define ASSUME(cond) __builtin_assume(cond)
#elif defined(__GNUC__)
#define ASSUME(cond)     \
  do {                   \
    if (!(cond))         \
      UNREACHABLE();     \
  } while (0)
#else
#define ASSUME(cond) ((void)0)
#endif

// ============================================================================
// Hot/Cold Function Attributes
// ============================================================================
// Guide compiler optimization heuristics

#if defined(__GNUC__) || defined(__clang__)
#define HOT __attribute__((hot))
#define COLD __attribute__((cold))
#else
#define HOT
#define COLD
#endif

// ============================================================================
// Pure/Const Function Attributes
// ============================================================================
// PURE: No side effects, result depends only on args and global state
// CONST: No side effects, result depends only on args (stricter than PURE)

#if defined(__GNUC__) || defined(__clang__)
#define PURE __attribute__((pure))
#define CONST_FN __attribute__((const))
#else
#define PURE
#define CONST_FN
#endif

// ============================================================================
// No Sanitizer Attributes
// ============================================================================
// Disable sanitizers for intentional undefined behavior (e.g., overflow)

#if defined(__clang__)
#define NO_SANITIZE_UNDEFINED __attribute__((no_sanitize("undefined")))
#define NO_SANITIZE_ADDRESS __attribute__((no_sanitize("address")))
#elif defined(__GNUC__) && __GNUC__ >= 8
#define NO_SANITIZE_UNDEFINED __attribute__((no_sanitize_undefined))
#define NO_SANITIZE_ADDRESS __attribute__((no_sanitize_address))
#else
#define NO_SANITIZE_UNDEFINED
#define NO_SANITIZE_ADDRESS
#endif

// ============================================================================
// Utility Macros
// ============================================================================

// Compile-time array size
template <typename T, size_t N>
constexpr size_t array_size(const T (&)[N]) noexcept {
  return N;
}

// Power of two rounding
constexpr size_t round_up_to_power_of_two(size_t n) noexcept {
  --n;
  n |= n >> 1;
  n |= n >> 2;
  n |= n >> 4;
  n |= n >> 8;
  n |= n >> 16;
  n |= n >> 32;
  return n + 1;
}

// Integer rounding
constexpr size_t round_up(size_t n, size_t alignment) noexcept {
  return (n + alignment - 1) / alignment * alignment;
}
