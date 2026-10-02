#pragma once

#include <chrono>
#include <cstdint>

namespace wirestead_bench {

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;
using Microseconds = std::chrono::microseconds;

inline TimePoint now() { return Clock::now(); }

inline double seconds_between(TimePoint start, TimePoint end) {
  return std::chrono::duration<double>(end - start).count();
}

inline int64_t elapsed_ns(TimePoint start, TimePoint end) {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
}

inline int64_t elapsed_us(TimePoint start, TimePoint end) {
  return std::chrono::duration_cast<Microseconds>(end - start).count();
}

}  // namespace wirestead_bench
