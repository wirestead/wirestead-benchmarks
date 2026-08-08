/*
 * Copyright 2025 Jinwoo Sung
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

// Tail latency under concurrent load, measured open loop.
//
// The other latency benchmarks here are closed loop: send, wait for the echo,
// send again. That shape cannot see a tail. When the system slows, the next
// send is delayed with it, so offered load falls exactly when the tail would
// have appeared and the run reports a latency the system never actually
// sustained. This benchmark fixes every message's send deadline before the run
// starts and measures from that intended time, so a late send is charged to the
// result rather than excused by it.
//
// Two workload shapes, because per-message optimisations need different
// conditions to engage at all:
//   --burst 1   one message in flight per connection. The write path has
//               nothing to gather, so this isolates per-message cost.
//   --burst 16  a batch queued per tick, so the gather write has something to
//               batch. 16 is the transport's own gather-buffer cap.
//
// Connections are the concurrency lever: each wirestead client owns an
// io_context and a thread, so connection count is thread count, and that is
// what makes allocator contention visible if it is there at all.
//
// READ THIS BEFORE TRUSTING A NUMBER: the sender threads are part of the
// instrument, and they saturate before the system does. lag_p99_us reports how
// late they were. If it is not small against p99_us, the run is measuring the
// harness. On a 20-core x86_64 box, 32 connections stayed honest to about
// 1000 msg/s/conn and lag_p99 blew past 170 us at 2000. Find the knee, then
// measure at half of it.

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "common/bench_config.hpp"
#include "common/result_writer.hpp"
#include "wirestead/wirestead.hpp"

namespace {

using Clock = std::chrono::steady_clock;
using Nanos = std::chrono::nanoseconds;

int64_t now_ns() { return std::chrono::duration_cast<Nanos>(Clock::now().time_since_epoch()).count(); }

// sleep_for overshoots by enough to rival the latency being measured, so wait
// on an absolute deadline and spin the last stretch. The spin window is CPU
// spent to keep the instrument's jitter well under the signal; it also bounds
// how many connections a run can drive before the senders become the
// bottleneck.
constexpr int64_t kSpinNs = 300'000;

void wait_until(int64_t deadline_ns) {
  const int64_t coarse = deadline_ns - kSpinNs;
  if (coarse > now_ns()) {
    struct timespec ts;
    ts.tv_sec = static_cast<time_t>(coarse / 1'000'000'000);
    ts.tv_nsec = static_cast<long>(coarse % 1'000'000'000);
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr) == EINTR) {
    }
  }
  while (now_ns() < deadline_ns) {
  }
}

// Every message carries its own deadline, so the receiving side can score it
// without sharing state with the sender.
struct Stamp {
  int64_t seq = 0;
  int64_t intended_ns = 0;
};

struct Connection {
  std::unique_ptr<wirestead::wrapper::TcpClient> client;
  std::string inbox;               // only the io thread for this connection touches these
  std::vector<int64_t> latencies;  //
  std::vector<int64_t> sched_lag;
  size_t sent = 0;
  size_t dropped = 0;
};

double percentile_us(std::vector<int64_t>& v, double p) {
  if (v.empty()) return 0.0;
  const size_t idx = static_cast<size_t>(std::llround(p / 100.0 * static_cast<double>(v.size() - 1)));
  std::nth_element(v.begin(), v.begin() + static_cast<long>(idx), v.end());
  return static_cast<double>(v[idx]) / 1000.0;
}

struct LoadConfig {
  uint16_t port = 9100;
  size_t connections = 32;
  size_t rate_per_conn = 500;
  size_t duration_ms = 5000;
  size_t payload_size = 256;
  size_t burst = 1;
  std::optional<std::string> csv_output;
};

LoadConfig parse(int argc, char** argv) {
  LoadConfig c;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    auto next = [&]() -> std::string {
      if (i + 1 >= argc) throw std::runtime_error("missing value for " + a);
      return argv[++i];
    };
    if (a == "--port") {
      c.port = static_cast<uint16_t>(std::stoul(next()));
    } else if (a == "--connections") {
      c.connections = std::stoul(next());
    } else if (a == "--rate-per-connection") {
      c.rate_per_conn = std::stoul(next());
    } else if (a == "--duration-ms") {
      c.duration_ms = std::stoul(next());
    } else if (a == "--payload-size") {
      c.payload_size = std::stoul(next());
    } else if (a == "--burst") {
      c.burst = std::stoul(next());
    } else if (a == "--csv-output") {
      c.csv_output = next();
    } else {
      throw std::runtime_error("unknown argument: " + a);
    }
  }
  if (c.payload_size < sizeof(Stamp)) throw std::runtime_error("--payload-size must be at least 16");
  if (c.burst == 0 || c.rate_per_conn == 0 || c.connections == 0) throw std::runtime_error("counts must be non-zero");
  return c;
}

void write_csv(const std::string& path, const LoadConfig& c, size_t sent, size_t dropped, size_t received,
               std::vector<int64_t>& lags, std::vector<int64_t>& lat) {
  const bool header = wirestead_bench::file_is_empty_or_missing(path);
  std::ofstream out(path, std::ios::app);
  if (!out) throw std::runtime_error("failed to open CSV output: " + path);
  if (header) {
    out << "transport,connections,rate_per_connection,burst,payload_size,duration_ms,sent,dropped,received,"
           "lag_p50_us,lag_p99_us,lag_max_us,p50_us,p99_us,p99_9_us,max_us\n";
  }
  out << "tcp," << c.connections << ',' << c.rate_per_conn << ',' << c.burst << ',' << c.payload_size << ','
      << c.duration_ms << ',' << sent << ',' << dropped << ',' << received << ',' << percentile_us(lags, 50) << ','
      << percentile_us(lags, 99) << ',' << percentile_us(lags, 100) << ',' << percentile_us(lat, 50) << ','
      << percentile_us(lat, 99) << ',' << percentile_us(lat, 99.9) << ',' << percentile_us(lat, 100) << '\n';
}

}  // namespace

int main(int argc, char** argv) {
  LoadConfig config;
  try {
    config = parse(argc, argv);
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 2;
  }

  auto server = wirestead::tcp_server(config.port).build();
  server->on_data([&server](const wirestead::MessageContext& ctx) { server->send_to(ctx.client_id(), ctx.data()); });
  if (!server->start_sync()) {
    std::cerr << "failed to start echo server on port " << config.port << '\n';
    return 1;
  }

  const size_t expected = config.duration_ms * config.rate_per_conn / 1000 + 1024;
  std::vector<std::unique_ptr<Connection>> conns;
  conns.reserve(config.connections);
  for (size_t i = 0; i < config.connections; ++i) {
    auto conn = std::make_unique<Connection>();
    Connection* raw = conn.get();
    raw->latencies.reserve(expected);
    raw->sched_lag.reserve(expected);

    auto client = wirestead::tcp_client(wirestead_bench::kDefaultHost, config.port).max_retries(50).build();
    const size_t payload = config.payload_size;
    client->on_data([raw, payload](const wirestead::MessageContext& ctx) {
      // Fixed-size records, so the echo stream splits without a framer.
      raw->inbox.append(ctx.data());
      while (raw->inbox.size() >= payload) {
        Stamp stamp{};
        std::memcpy(&stamp, raw->inbox.data(), sizeof(stamp));
        raw->latencies.push_back(now_ns() - stamp.intended_ns);
        raw->inbox.erase(0, payload);
      }
    });
    if (!client->start_sync()) {
      std::cerr << "connection " << i << " failed\n";
      return 1;
    }
    raw->client = std::move(client);
    conns.push_back(std::move(conn));
  }
  std::this_thread::sleep_for(std::chrono::milliseconds(300));

  // Warm up off the record: first-touch faults and lazy growth belong to setup,
  // not to the measured window.
  {
    const std::string warm(config.payload_size, 'w');
    for (auto& c : conns)
      for (int i = 0; i < 50; ++i) c->client->try_send(warm);
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    for (auto& c : conns) {
      c->latencies.clear();
      c->inbox.clear();
    }
  }

  const int64_t tick_ns = static_cast<int64_t>(1'000'000'000.0 * static_cast<double>(config.burst) /
                                               static_cast<double>(config.rate_per_conn));
  const int64_t epoch = now_ns() + 200'000'000;
  const size_t ticks = config.duration_ms * config.rate_per_conn / (1000 * config.burst);

  std::vector<std::thread> senders;
  senders.reserve(config.connections);
  for (size_t ci = 0; ci < config.connections; ++ci) {
    senders.emplace_back([&, ci] {
      Connection* c = conns[ci].get();
      std::string buf(config.payload_size, 'x');
      // Spread connections across the tick rather than firing them together.
      const int64_t offset = tick_ns * static_cast<int64_t>(ci) / static_cast<int64_t>(config.connections);
      for (size_t t = 0; t < ticks; ++t) {
        const int64_t deadline = epoch + offset + static_cast<int64_t>(t) * tick_ns;
        wait_until(deadline);
        c->sched_lag.push_back(now_ns() - deadline);
        for (size_t b = 0; b < config.burst; ++b) {
          const Stamp stamp{static_cast<int64_t>(t * config.burst + b), deadline};
          std::memcpy(buf.data(), &stamp, sizeof(stamp));
          if (c->client->try_send(buf)) {
            ++c->sent;
          } else {
            ++c->dropped;
          }
        }
      }
    });
  }
  for (auto& t : senders) t.join();

  std::this_thread::sleep_for(std::chrono::milliseconds(1500));  // let the tail drain
  for (auto& c : conns) c->client->stop();
  std::this_thread::sleep_for(std::chrono::milliseconds(300));
  server->stop();

  std::vector<int64_t> lat, lags;
  size_t sent = 0, dropped = 0;
  for (auto& c : conns) {
    lat.insert(lat.end(), c->latencies.begin(), c->latencies.end());
    lags.insert(lags.end(), c->sched_lag.begin(), c->sched_lag.end());
    sent += c->sent;
    dropped += c->dropped;
  }
  const size_t received = lat.size();

  std::cout << "transport: tcp\n"
            << "  connections: " << config.connections << '\n'
            << "  rate_per_connection: " << config.rate_per_conn << " msg/s\n"
            << "  burst: " << config.burst << '\n'
            << "  payload_size: " << config.payload_size << '\n'
            << "  sent: " << sent << "  dropped: " << dropped << "  received: " << received << '\n'
            << "  sender lag p50/p99/max us: " << percentile_us(lags, 50) << " / " << percentile_us(lags, 99) << " / "
            << percentile_us(lags, 100) << '\n'
            << "  latency p50/p99/p99.9/max us: " << percentile_us(lat, 50) << " / " << percentile_us(lat, 99) << " / "
            << percentile_us(lat, 99.9) << " / " << percentile_us(lat, 100) << '\n';

  if (config.csv_output) {
    write_csv(*config.csv_output, config, sent, dropped, received, lags, lat);
  }
  return 0;
}
