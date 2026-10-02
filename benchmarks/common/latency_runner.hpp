#pragma once

#include <condition_variable>
#include <cstdlib>
#include <fstream>
#ifdef WIRESTEAD_BENCH_REQUEST_TRACE
#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif
#endif
#include <iostream>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "common/bench_stats.hpp"
#include "common/bench_timer.hpp"
#include "common/payload.hpp"
#include "common/result_writer.hpp"

namespace wirestead_bench {

class EchoWaiter {
 public:
  void on_bytes(std::string_view bytes) {
    auto frames = decoder_.push(bytes);
    if (frames.empty()) {
      return;
    }

    {
      std::lock_guard<std::mutex> lock(mutex_);
      received_payload_ = std::move(frames.back());
      received_ = true;
    }
    cv_.notify_one();
  }

  void on_error(std::string_view message) {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      error_ = std::string(message);
    }
    cv_.notify_one();
  }

  void reset_iteration() {
    std::lock_guard<std::mutex> lock(mutex_);
    received_ = false;
    received_payload_.clear();
  }

  std::string wait_for_echo(std::chrono::milliseconds timeout) {
    std::unique_lock<std::mutex> lock(mutex_);
    const bool ready = cv_.wait_for(lock, timeout, [this] { return received_ || !error_.empty(); });

    if (!error_.empty()) {
      throw std::runtime_error(error_);
    }
    if (!ready) {
      throw std::runtime_error("timed out waiting for echo");
    }

    return received_payload_;
  }

 private:
  FrameDecoder decoder_;
  std::mutex mutex_;
  std::condition_variable cv_;
  bool received_ = false;
  std::string received_payload_;
  std::string error_;
};

template <typename Client>
int run_latency_client(std::string_view transport, Client& client, size_t payload_size, size_t iterations,
                       size_t warmup_iterations = 0,
                       const std::optional<std::string>& csv_output = std::nullopt) {
#ifndef WIRESTEAD_BENCH_REQUEST_TRACE
  if (std::getenv("WIRESTEAD_REQUEST_TRACE")) {
    throw std::runtime_error("request tracing requires WIRESTEAD_BENCH_REQUEST_TRACE=ON");
  }
#endif
  if (!client.start_sync()) {
    std::cerr << "Failed to start " << transport << " client\n";
    return 1;
  }

  const std::string payload = make_payload(payload_size);
  const std::string frame = make_frame(payload);
  const char* samples_path = std::getenv("WIRESTEAD_LATENCY_SAMPLES");
  std::vector<int64_t> samples;
  samples.reserve(iterations);
#ifdef WIRESTEAD_BENCH_REQUEST_TRACE
  const char* trace_path = std::getenv("WIRESTEAD_REQUEST_TRACE");
  struct RequestTrace { int64_t start, submitted, end; };
  std::vector<RequestTrace> traces;
  if (trace_path) traces.reserve(iterations);
  const auto nanoseconds = [](TimePoint t) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(t.time_since_epoch()).count();
  };
#endif

  auto run_iteration = [&](size_t i, bool record_sample) {
    auto& waiter = client.echo_waiter();
    waiter.reset_iteration();

    const auto start = now();
    if (!client.send_frame(frame)) {
      std::cerr << "Failed to send payload at iteration " << i << "\n";
      client.stop();
      return false;
    }

#ifdef WIRESTEAD_BENCH_REQUEST_TRACE
    const auto submitted = now();
#endif
    const std::string echoed = waiter.wait_for_echo(std::chrono::seconds(5));
    const auto end = now();

    if (!payload_matches(payload, echoed)) {
      std::cerr << "Echo payload mismatch at iteration " << i << "\n";
      client.stop();
      return false;
    }

    if (record_sample) {
      samples.push_back(elapsed_ns(start, end));
#ifdef WIRESTEAD_BENCH_REQUEST_TRACE
      if (trace_path) traces.push_back({nanoseconds(start), nanoseconds(submitted), nanoseconds(end)});
#endif
    }
    return true;
  };

  for (size_t i = 0; i < warmup_iterations; ++i) {
    if (!run_iteration(i, false)) {
      client.stop();
      return 1;
    }
  }

  const auto total_start = now();
  for (size_t i = 0; i < iterations; ++i) {
    if (!run_iteration(i, true)) {
      client.stop();
      return 1;
    }
  }
  const auto total_end = now();

  client.stop();

#ifdef WIRESTEAD_BENCH_REQUEST_TRACE
  if (trace_path) {
    std::ofstream output(trace_path);
#ifdef _WIN32
    const auto pid = _getpid();
#else
    const auto pid = getpid();
#endif
    output << "pid,iteration,start_ns,send_return_ns,end_ns\n";
    for (size_t i = 0; i < traces.size(); ++i)
      output << pid << ',' << i << ',' << traces[i].start << ',' << traces[i].submitted << ','
             << traces[i].end << '\n';
    if (!output) throw std::runtime_error("request trace write failed");
  }
#endif
  // Export the same timed samples after the run; keep legacy CSV values in
  // truncated microseconds. Recording does not add clocks or per-request I/O.
  if (samples_path) {
    std::ofstream output(samples_path);
    output << "iteration,rtt_ns\n";
    for (size_t i = 0; i < samples.size(); ++i) output << i << ',' << samples[i] << '\n';
    output.close();
    if (!output) throw std::runtime_error("latency sample write failed");
  }
  for (auto& sample : samples) sample /= 1000;
  const auto result = make_latency_result(transport, payload_size, iterations, warmup_iterations,
                                          seconds_between(total_start, total_end),
                                          compute_latency_stats(std::move(samples)));
  print_result(result, csv_output);
  return 0;
}

}  // namespace wirestead_bench
