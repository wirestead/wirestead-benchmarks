# Controlled TCP throughput

`bench_tcp_controlled` is an opt-in Linux benchmark for one TCP strategy per process. Enable `-DWIRESTEAD_BENCH_CONTROLLED_TCP=ON`. It requires the modern wrapper external-context constructors, `manage_external_context`, server `shared_context`, resettable runtime stats, and C++20. Default builds and the existing six-phase matrix are unchanged.

Example:

```sh
./build/bin/bench_tcp_controlled --strategy besteffort --main-cpu 0 --sender-cpu 1 --client-cpu 2 --server-cpu 3 --payload-size 1024 --duration-ms 3000 --warmup-messages 512 --csv-output result.csv
```

Use `reliable` or `besteffort`; each invocation produces exactly one CSV row. The CSV path is overwritten, so use a unique file per trial. Choose CPUs available on the host. Every role pins itself, reads back the affinity mask, checks the current CPU, and emits its role, Linux TID, CPU and verification result. Invalid CPU assignments fail the run. Do not use the ordinal-based affinity preload with this target.

The benchmark owns one client io_context worker and one shared server io_context worker; the server session uses the server context. The sender and controller have their own explicit CPUs. Library auxiliary threads such as the resolver may still exist. This topology is intentionally different from the default strategy matrix; do not compute improvements by mixing the two fixtures.

The same sender thread sends the warmup and measurement traffic. Before releasing the start gate, all warmup bytes must be received, reported as sent, and absent from queued/pending stats. Runtime stats are then reset and received bytes cleared. The sender records its actual start and end; throughput divides by actual elapsed time, which is recorded alongside requested duration. A send already in progress may finish after the stop timer. After sending, the fixture waits up to 10 seconds for accepted bytes to be received and write completions to drain. Warmup is excluded from the measured counters.

New-input refusal is recorded as failed_sends; legacy drop counters are reported separately because they can include such refusals. Success requires accepted-byte and request counters to match the send loop. If the core exposes send_accounting, success also requires all accepted requests/bytes to be written with zero outstanding. An unavailable ledger is explicitly marked unsupported rather than reported as zero. Affinity, start, warmup, drain and accounting failures return nonzero.

Build and run checks:

```sh
cmake --build build --target bench_tcp_controlled -j2
python3 tests/test_tcp_controlled.py build/bin/bench_tcp_controlled -v
```

The integration check runs both strategies, verifies all four role records and delivery, exercises invalid options, and checks worker pin/start failure cleanup. General unittest discovery skips these opt-in integration checks when no executable was supplied.

For comparisons, use identical fixture source/flags and unchanged core libraries, unique output paths, serial ABBA trials (at least 6 per version/condition), and retain role records plus hardware/clock evidence. This is a throughput fixture; it does not measure p99 or prove loaded latency, memory or fairness gates.

## Repeated comparison

```sh
python3 scripts/compare_controlled_tcp.py --baseline-build /path/to/baseline --candidate-build /path/to/candidate --main-cpu 0 --sender-cpu 1 --client-cpu 2 --server-cpu 3 --rounds 3 --output /path/to/new-results
```

The runner creates a fresh output directory, runs both strategies in separate processes, checks role/TID evidence and delivery/ledger fields, fingerprints binaries and linked libraries before/after, and retains telemetry and failures. It removes diagnostic preloads and library overrides. It does not change clocks or invoke sudo; use the existing controlled clock runner when required. A shared layout can assign sender and client to the same CPU in a separate comparison.
