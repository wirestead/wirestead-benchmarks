#include <exception>
#include <iostream>
#include <memory>
#include <string>

#include "common/bench_config.hpp"
#include "common/latency_runner.hpp"
#include "wirestead_bench_target.hpp"

class TcpBenchClient {
 public:
  explicit TcpBenchClient(const wirestead_bench::NetworkConfig& config) {
    client_ =
        wirestead::tcp_client(config.host, config.port)
            .on_data([this](const wirestead::MessageContext& ctx) { waiter_.on_bytes(ctx.data()); })
            .on_error([this](const wirestead::ErrorContext& ctx) { waiter_.on_error(ctx.message()); })
            .max_retries(0)
            .build();
  }

  bool start_sync() { return client_->start_sync(); }
  bool send_frame(std::string_view frame) { return static_cast<bool>(client_->send_blocking(frame)); }
  void stop() { client_->stop(); }
  wirestead_bench::EchoWaiter& echo_waiter() { return waiter_; }

 private:
  wirestead_bench::EchoWaiter waiter_;
  std::unique_ptr<wirestead::TcpClient> client_;
};

int main(int argc, char** argv) {
  try {
    const auto config = wirestead_bench::parse_network_client_args(argc, argv, wirestead_bench::kDefaultTcpPort);
    TcpBenchClient client(config);
    return wirestead_bench::run_latency_client("tcp", client, config.payload_size, config.iterations,
                                             config.warmup_iterations, config.csv_output);
  } catch (const std::exception& ex) {
    std::cerr << ex.what() << "\n";
    return 1;
  }
}
