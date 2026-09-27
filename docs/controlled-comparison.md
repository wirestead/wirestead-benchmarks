# Controlled comparisons and optional diagnostics

Use this workflow to compare two prebuilt versions on the **same Linux host**.
It does not publish a release or change the Wirestead library.

## Build and compare

Build this benchmark revision twice, with the same compiler, dependencies and
Release options, against the two desired Wirestead refs. For example:

```sh
cmake -S . -B build-v096 -DWIRESTEAD_BENCH_USE_FETCHCONTENT=ON \
  -DWIRESTEAD_BENCH_GIT_TAG=v0.9.6 -DCMAKE_BUILD_TYPE=Release
cmake --build build-v096 -j2
cmake -S . -B build-current -DWIRESTEAD_BENCH_USE_FETCHCONTENT=ON \
  -DWIRESTEAD_BENCH_SOURCE_DIR=/absolute/path/to/wirestead -DCMAKE_BUILD_TYPE=Release
cmake --build build-current -j2
python3 scripts/compare_builds.py --baseline-build build-v096 \
  --candidate-build build-current --output results/comparison-001
python3 scripts/summarize_comparison.py results/comparison-001
```

The output directory must not exist. Never reuse a prior result directory.
Use an ordinary user. The runner uses private child process groups and terminates
them on timeout/interruption. It owns and cleans up only its own echo servers
and temporary UDS socket. Chosen loopback ports have a small bind race; a conflict
fails the trial rather than being silently retried or omitted.

Defaults: payloads 64/1024/4096 bytes, one ABBA round (two measurements per version),
three seconds per matrix phase, 10,000 measured latency requests and 1,000 warmup
requests. UDP latency is capped at 1024 bytes; matrix UDP still covers all payloads.
ABBA means baseline, candidate, candidate, baseline. Increase `--abba-rounds` for
more repetitions. `--suite latency` and `--suite matrix` select one suite.
The matrix always contains all six TCP/UDP/UDS × Reliable/BestEffort phases;
`--transports` applies only to latency.

Metadata includes benchmark/source Git revisions and dirty state when available,
build directories, CMake cache/source metadata, executable and
local shared-library hashes, and the existing hardware/environment collection.
Binary hashes are checked again at completion. Output files have a completion
manifest; the summarizer verifies their hashes before reporting. This cannot prove that an installed
external library or recorded source commit was clean or built with matching
flags. Inspect the recorded configuration and keep builds unchanged throughout.

Each trial has unique CSV/log/telemetry files. Frequency and temperature samples
are observations, not automatic acceptance criteria. The summary refuses failed,
incomplete or unbalanced runs and retains all raw admission/delivery/drop fields.
It reports the median and range across runs; a median of per-run p99 values is
**not** a pooled p99. No outlier is removed. No automatic performance threshold
or release approval is inferred. Legacy drop fields cannot identify accepted-work
loss causes, and UDP Reliable admission is not a network delivery guarantee.

## CPU placement

`--matrix-cpus 0-3` constrains the whole matrix process. This is not equivalent
to pinning individual workers. For the creation-order placement used in the Orin
investigation, build the optional preload helper:

```sh
cc -O2 -std=c11 -shared -fPIC scripts/diagnostics/thread_affinity.c \
  -o /absolute/path/thread_affinity.so -ldl -pthread
python3 scripts/compare_builds.py --baseline-build build-v096 \
  --candidate-build build-current --output results/placement-001 \
  --matrix-preload /absolute/path/thread_affinity.so \
  --matrix-main-cpu 0 --matrix-worker-cpus 1,2,3,4 \
  --server-cpu 1 --client-cpu 2
```

Worker CPUs repeat cyclically by pthread creation ordinal. Check
`MEASUREMENT_PIN` log lines in both versions: ordinal roles are not a stable API.
CPU numbering/cache topology differs across hardware. On the measured Orin,
`1,2,3,4` separated sender and I/O L3 domains; `1,2,0,3` shared an L3 domain.
Use separate output directories for each layout. The helper is loaded only into
matrix subprocesses; echo latency has separate server/client CPU options.

## Explicit fixed CPU clocks

The default runner does not change clocks. The separate controller requires sudo
and runs the benchmark command as the specified non-root user:

```sh
sudo python3 scripts/with_fixed_clocks.py --user "$USER" \
  --log-dir /absolute/path/results/clock-001 -- \
  python3 /absolute/path/scripts/compare_builds.py \
    --baseline-build /absolute/path/build-v096 \
    --candidate-build /absolute/path/build-current \
    --output /absolute/path/results/comparison-002
```

Use paths writable by that user. `--check` is a read-only preflight.
The controller records existing minima/maxima/governors, raises CPU minima to
the existing maxima, and restores the original minima on normal exit, child
failure, timeout or handled SIGINT/SIGTERM/SIGHUP. Readback is polled for up to
five seconds, including restoration. A persistent mismatch is an error retained
in `clock-after.json`. SIGKILL/power loss cannot run cleanup; retain
`clock-before.json` for manual recovery. GPU/EMC clocks, thermal management and
background services are not changed. Avoid simultaneous benchmarks; a system lock
serializes this controller, not unrelated commands that change power settings.
Check clock evidence in addition to the child's `completed.json`.

## Request timelines and scheduler tracing (diagnostic only)

Build **both versions** with `-DWIRESTEAD_BENCH_REQUEST_TRACE=ON`, then use
`compare_builds.py --suite latency --request-traces`.
Tracing is compiled out by default. Setting the trace environment variable on a
normal build fails explicitly. Diagnostic builds add an extra timestamp in the
loop, so do not mix them with normal-build performance baselines.

The runner sets `WIRESTEAD_REQUEST_TRACE` to a unique CSV per trial. Clients
preallocate records and write them after measurement/stop. Columns are process ID,
iteration, start, send-return and completion nanoseconds on the steady clock.
The Linux runner verifies row counts and monotonic-clock observation bounds.

For scheduler evidence, add `--perf /absolute/path/to/perf` to the clock controller.
It checks tracepoint availability before changing clocks and records system-wide
sched_switch, sched_wakeup, sched_wakeup_new and sched_migrate_task with the
monotonic clock. Benchmark commands remain unprivileged. Run a separate untraced
diagnostic control with identical options to assess tracing overhead.

Capture includes background task names and scheduling metadata, not application
payloads. Clocks are restored before decoding; the data is decoded as its owner.
Inspect `command.log`, `decode.json` and lost-record warnings. Raw `sched.data`
is retained if decoding fails. A working kernel-compatible perf/tracefs setup is
required; no packages or sysctls are modified automatically.

Correlate request intervals with raw scheduler timestamps and actual client/server
thread IDs. Runnable off-CPU intervals indicate execution delay; blocked intervals
alone do not distinguish a mutex from socket/condition-variable waiting.
The request PID identifies the main thread, not all transport workers. Do not add
overlapping waits across threads or label residual time as pure user CPU time.
One traced outlier cannot establish the cause of an earlier untraced outlier.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

Unit tests use temporary fake cpufreq files, never real clock settings. CI also
builds diagnostic and normal configurations against v0.9.6 and main and runs a
small functional self-comparison. That self-comparison validates tooling; it is
not a performance qualification.
