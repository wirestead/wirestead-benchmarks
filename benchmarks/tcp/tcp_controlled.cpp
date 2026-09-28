// Controlled TCP throughput fixture. Explicit executor ownership is
// intentional: these results must not be compared directly with the default
// strategy matrix.
#include "wirestead_bench_target.hpp"
#include <atomic>
#include <wirestead/concurrency/io_thread_hook.hpp>

#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <fstream>
#include <future>
#include <iomanip>
#include <iostream>
#include <limits>
#include <mutex>
#include <optional>
#include <pthread.h>
#include <sched.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <syncstream>
#include <sys/syscall.h>
#include <thread>
#include <unistd.h>
#include <utility>

using Clock = std::chrono::steady_clock;

struct Config {
  std::string strategy, csv;
  int main_cpu = -1, sender_cpu = -1, client_cpu = -1, server_cpu = -1;
  size_t payload = 1024, warmup = 512;
  unsigned duration_ms = 3000, port = 19290;
};
unsigned number(const std::string &s) {
  unsigned n = 0;
  auto [end, ec] = std::from_chars(s.data(), s.data() + s.size(), n);
  if (ec != std::errc{} || end != s.data() + s.size())
    throw std::invalid_argument("invalid integer: " + s);
  return n;
}
Config parse(int argc, char **argv) {
  Config c;
  for (int i = 1; i < argc; ++i) {
    std::string key = argv[i];
    if (++i == argc)
      throw std::invalid_argument("missing value for " + key);
    std::string value = argv[i];
    if (key == "--strategy")
      c.strategy = value;
    else if (key == "--csv-output")
      c.csv = value;
    else {
      auto n = number(value);
      if (key == "--payload-size")
        c.payload = n;
      else if (key == "--warmup-messages")
        c.warmup = n;
      else if (key == "--duration-ms")
        c.duration_ms = n;
      else if (key == "--port")
        c.port = n;
      else {
        if (n >= CPU_SETSIZE)
          throw std::invalid_argument("CPU exceeds CPU_SETSIZE");
        if (key == "--main-cpu")
          c.main_cpu = static_cast<int>(n);
        else if (key == "--sender-cpu")
          c.sender_cpu = static_cast<int>(n);
        else if (key == "--client-cpu")
          c.client_cpu = static_cast<int>(n);
        else if (key == "--server-cpu")
          c.server_cpu = static_cast<int>(n);
        else
          throw std::invalid_argument("unknown option: " + key);
      }
    }
  }
  if ((c.strategy != "reliable" && c.strategy != "besteffort") ||
      c.main_cpu < 0 || c.sender_cpu < 0 || c.client_cpu < 0 ||
      c.server_cpu < 0 || !c.payload || !c.warmup || !c.duration_ms ||
      !c.port || c.port > 65535 ||
      c.payload > std::numeric_limits<uint64_t>::max() / c.warmup)
    throw std::invalid_argument(
        "require strategy reliable|besteffort, four CPUs, positive "
        "payload/warmup/duration and valid port");
  return c;
}
void pin(const char *role, int cpu) {
  cpu_set_t mask;
  CPU_ZERO(&mask);
  CPU_SET(cpu, &mask);
  if (sched_setaffinity(0, sizeof(mask), &mask))
    throw std::runtime_error(std::string("cannot pin ") + role);
  CPU_ZERO(&mask);
  if (sched_getaffinity(0, sizeof(mask), &mask) || CPU_COUNT(&mask) != 1 ||
      !CPU_ISSET(cpu, &mask) || sched_getcpu() != cpu)
    throw std::runtime_error(std::string("affinity verification failed: ") +
                             role);
  pthread_setname_np(pthread_self(), role);
  std::osyncstream(std::cout)
      << "ROLE role=" << role << " tid=" << syscall(SYS_gettid)
      << " cpu=" << cpu << " verified=1\n";
}
struct RoleState {
  std::promise<void> ready;
  std::future<void> future = ready.get_future();
  std::atomic<unsigned> calls{0};
  std::atomic<long> tid{0};
  void wait() {
    if (future.wait_for(std::chrono::seconds(2)) != std::future_status::ready)
      throw std::runtime_error("missing executor role hook");
    future.get();
    if (calls.load() != 1)
      throw std::runtime_error("unexpected executor thread count");
  }
};
struct HookScope {
  ~HookScope() { wirestead::concurrency::set_io_thread_init(nullptr); }
};
std::shared_ptr<RoleState> install_role(const char *role, int cpu) {
  auto state = std::make_shared<RoleState>();
  wirestead::concurrency::set_io_thread_init([state, role, cpu] {
    if (state->calls.fetch_add(1) != 0)
      return;
    try {
      pin(role, cpu);
      state->tid.store(syscall(SYS_gettid));
      state->ready.set_value();
    } catch (...) {
      state->ready.set_exception(std::current_exception());
    }
  });
  return state;
}
template <class Client>
void drain(Client &client, const std::atomic<uint64_t> &received,
           uint64_t bytes) {
  const auto deadline = Clock::now() + std::chrono::seconds(10);
  for (;;) {
    auto stats = client.stats();
    if (received.load() == bytes && stats.bytes_sent == bytes &&
        !stats.queued_bytes && !stats.pending_bytes)
      return;
    if (Clock::now() >= deadline)
      throw std::runtime_error(
          "TCP drain timed out; accepted data or completion missing");
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
}
template <class Stats>
std::optional<uint64_t> validate_ledger(const Stats &stats) {
  if constexpr (requires { stats.send_accounting; }) {
    if (!stats.send_accounting)
      throw std::runtime_error("missing send ledger");
    const auto &a = *stats.send_accounting;
    if (a.outstanding.requests || a.outstanding.bytes ||
        a.accepted.requests != a.written.requests ||
        a.accepted.bytes != a.written.bytes)
      throw std::runtime_error("accepted requests not fully written in ledger");
    return a.outstanding.requests;
  }
  return std::nullopt;
}
int run(const Config &c) {
  pin("bench-main", c.main_cpu);
  HookScope hooks;
  auto server_role = install_role("bench-server", c.server_cpu);
  std::atomic<uint64_t> received{0};
  std::atomic<bool> server_callback_ok{false}, client_callback_ok{false};
  wirestead::TcpServer server(static_cast<uint16_t>(c.port));
  server.on_data([&, checked =
                         false](const wirestead::MessageContext &ctx) mutable {
    if (!checked) {
      checked = true;
      server_callback_ok.store(syscall(SYS_gettid) == server_role->tid.load() &&
                               sched_getcpu() == c.server_cpu);
      std::osyncstream(std::cout)
          << "CALLBACK_ROLE role=bench-server tid=" << syscall(SYS_gettid)
          << " cpu=" << sched_getcpu()
          << " verified=" << server_callback_ok.load() << '\n';
    }
    received.fetch_add(ctx.data().size(), std::memory_order_relaxed);
  });
  if (!server.start_sync())
    throw std::runtime_error("server start failed");
  server_role->wait();
  auto client_role = install_role("bench-client", c.client_cpu);
  wirestead::TcpClient client("127.0.0.1", static_cast<uint16_t>(c.port));
  client.backpressure_strategy(
      c.strategy == "reliable"
          ? wirestead::base::constants::BackpressureStrategy::Reliable
          : wirestead::base::constants::BackpressureStrategy::BestEffort);
  client.on_connect([&](const auto &) {
    client_callback_ok.store(syscall(SYS_gettid) == client_role->tid.load() &&
                             sched_getcpu() == c.client_cpu);
    std::osyncstream(std::cout)
        << "CALLBACK_ROLE role=bench-client tid=" << syscall(SYS_gettid)
        << " cpu=" << sched_getcpu()
        << " verified=" << client_callback_ok.load() << '\n';
  });
  if (!client.start_sync())
    throw std::runtime_error("client start failed");
  client_role->wait();
  std::mutex mutex;
  std::condition_variable_any cv;
  bool go = false;
  std::atomic<bool> running{true};
  std::promise<void> warm, finished;
  std::promise<Clock::time_point> started;
  auto warm_future = warm.get_future();
  auto start_future = started.get_future();
  auto finish_future = finished.get_future();
  uint64_t accepted = 0, failed = 0;
  Clock::time_point end;
  std::jthread sender([&](std::stop_token token) {
    try {
      pin("bench-sender", c.sender_cpu);
      const std::string payload(c.payload, 'A');
      for (size_t i = 0; i < c.warmup; ++i)
        if (!client.send(payload))
          throw std::runtime_error("warmup send rejected");
      warm.set_value();
      {
        std::unique_lock lock(mutex);
        if (!cv.wait(lock, token, [&] { return go; }))
          return;
      }
      started.set_value(Clock::now());
      while (running.load(std::memory_order_relaxed) &&
             !token.stop_requested()) {
        if (client.send(payload))
          ++accepted;
        else {
          ++failed;
          std::this_thread::sleep_for(std::chrono::microseconds(50));
        }
      }
      end = Clock::now();
      finished.set_value();
    } catch (...) {
      auto error = std::current_exception();
      try {
        warm.set_exception(error);
      } catch (const std::future_error &) {
      }
      try {
        started.set_exception(error);
      } catch (const std::future_error &) {
      }
      try {
        finished.set_exception(error);
      } catch (const std::future_error &) {
      }
    }
  });
  warm_future.get();
  drain(client, received, c.warmup * c.payload);
  if (!server_callback_ok.load() || !client_callback_ok.load() ||
      server_role->calls.load() != 1 || client_role->calls.load() != 1)
    throw std::runtime_error("callback executor role mismatch");
  client.reset_stats();
  server.reset_stats();
  received.store(0);
  {
    std::lock_guard lock(mutex);
    go = true;
  }
  cv.notify_all();
  const auto start = start_future.get();
  std::this_thread::sleep_until(start +
                                std::chrono::milliseconds(c.duration_ms));
  running.store(false, std::memory_order_relaxed);
  sender.join();
  finish_future.get();
  drain(client, received, accepted * c.payload);
  const auto stats = client.stats();
  if (stats.messages_accepted != accepted ||
      stats.bytes_accepted != accepted * c.payload)
    throw std::runtime_error("send accounting mismatch");
  const auto outstanding = validate_ledger(stats);
  client.stop();
  server.stop();
  const auto elapsed =
      std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
  const double throughput = static_cast<double>(received.load()) /
                            (1024. * 1024.) /
                            (static_cast<double>(elapsed) / 1e9);
  std::ostringstream row;
  row << "tcp," << c.strategy << ',' << c.payload << ',' << c.duration_ms << ','
      << elapsed << ',' << c.warmup << ',' << c.main_cpu << ',' << c.sender_cpu
      << ',' << c.client_cpu << ',' << c.server_cpu << ',' << accepted << ','
      << failed << ',' << accepted * c.payload << ',' << received.load() << ','
      << std::fixed << std::setprecision(6) << throughput << ','
      << stats.messages_sent << ',' << stats.queued_bytes << ','
      << stats.pending_bytes << ',' << stats.dropped_messages << ','
      << stats.dropped_bytes << ',' << (outstanding ? 1 : 0) << ','
      << (outstanding ? std::to_string(*outstanding) : std::string{}) << '\n';
  std::cout << "RESULT " << row.str();
  if (!c.csv.empty()) {
    std::ofstream out(c.csv, std::ios::trunc);
    if (!out)
      throw std::runtime_error("cannot open CSV");
    out << "transport,strategy,payload_size,duration_ms,elapsed_ns,warmup_"
           "messages,main_cpu,sender_cpu,client_cpu,server_cpu,accepted_"
           "messages,failed_sends,accepted_bytes,received_bytes,received_mib_"
           "sec,client_messages_sent,client_queued_bytes_final,client_pending_"
           "bytes_final,client_dropped_messages,client_dropped_bytes,"
           "accounting_supported,outstanding_requests_final\n"
        << row.str();
    out.flush();
    if (!out)
      throw std::runtime_error("cannot write CSV");
  }
  return 0;
}
int main(int argc, char **argv) {
  try {
    return run(parse(argc, argv));
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
