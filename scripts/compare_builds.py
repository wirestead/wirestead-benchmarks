#!/usr/bin/env python3
"""Linux, serial ABBA comparisons of two already-built benchmark suites."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

from collect_environment import collect_environment

LABELS = ("baseline", "candidate")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def read(path):
    try:
        # Some sysfs drivers temporarily return EAGAIN. Binary reads can
        # return None; TextIOWrapper on Python 3.10 raises TypeError instead.
        data = path.read_bytes()
        return None if data is None else data.decode("utf-8").strip()
    except (OSError, UnicodeError):
        return None


def telemetry(pids=()):
    threads = {}
    for pid in pids:
        threads[str(pid)] = {}
        for task in Path(f"/proc/{pid}/task").glob("*"):
            status = read(task / "status")
            if status:
                threads[str(pid)][task.name] = [
                    line for line in status.splitlines()
                    if line.startswith(("Name:", "Cpus_allowed_list:"))]
    return {
        "threads": threads,
        "monotonic_ns": time.monotonic_ns(),
        "frequency_khz": {p.name: read(p / "scaling_cur_freq")
                          for p in Path("/sys/devices/system/cpu/cpufreq").glob("policy*")},
        "temperature_millic": {p.name: read(p / "temp")
                               for p in Path("/sys/class/thermal").glob("thermal_zone*")},
    }


def stop(proc):
    if proc is None:
        return
    # All spawned commands have a private process group, including descendants.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def command(binary, cpu=None):
    return (["taskset", "-c", str(cpu)] if cpu is not None else []) + [str(binary)]


def execute(cmd, prefix, timeout, env=None, extra_pids=()):
    with prefix.with_suffix(".log").open("w") as log, prefix.with_suffix(".jsonl").open("w") as samples:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while proc.poll() is None:
                samples.write(json.dumps(telemetry([proc.pid, *extra_pids])) + "\n")
                samples.flush()
                if time.monotonic() >= deadline:
                    raise TimeoutError("command timed out: " + str(cmd))
                time.sleep(.1)
            if proc.returncode:
                raise RuntimeError(f"command exited {proc.returncode}: {cmd}")
        finally:
            stop(proc)
    return proc.pid


def rows(path):
    with path.open() as stream:
        result = list(csv.DictReader(stream))
    if not result or any(None in row.values() for row in result):
        raise ValueError(f"incomplete CSV: {path}")
    return result


def ready(server, transport, address):
    deadline = time.monotonic() + 5
    if transport == "udp":
        time.sleep(.3)
        if server.poll() is not None:
            raise RuntimeError("UDP echo server exited")
        return
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError("echo server exited")
        try:
            if transport == "uds":
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.connect(address)
            else:
                with socket.create_connection(("127.0.0.1", address), timeout=.1):
                    pass
            return
        except OSError:
            time.sleep(.05)
    raise TimeoutError("echo server did not become ready")


def latency(args, build, name, transport, size):
    prefix = args.output / name
    start_ns = time.monotonic_ns()
    with tempfile.TemporaryDirectory(prefix="wsbench-") as tmp:
        if transport == "uds":
            address = str(Path(tmp) / "echo.sock")
            flags = ["--path", address]
        else:
            kind = socket.SOCK_DGRAM if transport == "udp" else socket.SOCK_STREAM
            with socket.socket(socket.AF_INET, kind) as sock:
                sock.bind(("127.0.0.1", 0))
                address = sock.getsockname()[1]
            flags = ["--port", str(address)]
        with (args.output / (name + "-server.log")).open("w") as log:
            server = subprocess.Popen(
                command(build / "bin" / f"bench_{transport}_echo_server", args.server_cpu) + flags,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                ready(server, transport, address)
                client_flags = flags if transport == "uds" else ["--host", "127.0.0.1"] + flags
                env = dict(os.environ)
                env.pop("WIRESTEAD_REQUEST_TRACE", None)
                env.pop("WIRESTEAD_LATENCY_SAMPLES", None)
                env.pop("LD_PRELOAD", None)
                if args.latency_samples:
                    env["WIRESTEAD_LATENCY_SAMPLES"] = str(args.output / (name + "-samples.csv"))
                if args.request_traces:
                    env["WIRESTEAD_REQUEST_TRACE"] = str(args.output / (name + "-requests.csv"))
                client_pid = execute(command(build / "bin" / f"bench_{transport}_latency_client", args.client_cpu)
                        + client_flags + ["--payload-size", str(size), "--iterations", str(args.iterations),
                                          "--warmup-iterations", str(args.warmup),
                                          "--csv-output", str(prefix.with_suffix(".csv"))],
                        prefix, args.timeout, env, [server.pid])
            finally:
                stop(server)
    end_ns = time.monotonic_ns()
    (args.output / (name + "-window.json")).write_text(json.dumps({
        "start_ns": start_ns, "end_ns": end_ns, "client_pid": client_pid, "server_pid": server.pid}))
    if args.latency_samples:
        samples = rows(args.output / (name + "-samples.csv"))
        if len(samples) != args.iterations or any(
                int(x["iteration"]) != i or int(x["rtt_ns"]) < 0
                for i, x in enumerate(samples)):
            raise ValueError("latency sample count or value mismatch")
    if args.request_traces:
        trace = rows(args.output / (name + "-requests.csv"))
        if len(trace) != args.iterations or not all(
                start_ns <= int(x["start_ns"]) <= int(x["send_return_ns"]) <= int(x["end_ns"]) <= end_ns
                for x in trace):
            raise ValueError("request trace count or monotonic time bounds mismatch")
    data = rows(prefix.with_suffix(".csv"))
    if len(data) != 1 or int(data[0]["iterations"]) != args.iterations:
        raise ValueError("latency output count mismatch")


def git_state(path):
    try:
        head = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(path), "status", "--porcelain"], text=True)
        diff = subprocess.check_output(["git", "-C", str(path), "diff", "HEAD"])
        return {"commit": head, "status": dirty, "tracked_diff_sha256": hashlib.sha256(diff).hexdigest()}
    except (OSError, subprocess.CalledProcessError):
        return None


def manifest(build):
    files = [p for p in (build / "bin").glob("bench_*") if p.is_file()]
    files += [p for p in build.rglob("libwirestead.so*") if p.is_file()]
    files += [p for p in build.rglob("libunilink.so*") if p.is_file()]
    metadata_path = build / "wirestead_bench_metadata.csv"
    source_metadata = read(metadata_path)
    source = {}
    if source_metadata:
        source = dict(csv.reader(source_metadata.splitlines()[1:]))
    source_path = source.get("wirestead_source_path")
    return {
        "source_git": git_state(source_path) if source_path else None,
        "build": str(build),
        "files": {str(p): digest(p) for p in files},
        "cmake_cache": read(build / "CMakeCache.txt"),
        "source_metadata": read(build / "wirestead_bench_metadata.csv"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-build", type=Path, required=True)
    parser.add_argument("--candidate-build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--payloads", type=int, nargs="+", default=[64, 1024, 4096])
    parser.add_argument("--transports", choices=["tcp", "udp", "uds"], nargs="+", default=["tcp", "udp", "uds"])
    parser.add_argument("--suite", choices=["all", "matrix", "latency"], default="all")
    parser.add_argument("--abba-rounds", type=int, default=1)
    parser.add_argument("--duration", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--warmup", type=int, default=1000)
    parser.add_argument("--udp-max-payload", type=int, default=1024)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--matrix-cpus", help="taskset CPU list for the whole matrix process")
    parser.add_argument("--matrix-preload", type=Path, help="optional Linux thread-affinity helper")
    parser.add_argument("--matrix-main-cpu", type=int)
    parser.add_argument("--matrix-worker-cpus", help="comma-separated creation-order worker CPUs for the helper")
    parser.add_argument("--server-cpu", type=int)
    parser.add_argument("--client-cpu", type=int)
    parser.add_argument("--latency-samples", action="store_true",
                        help="write post-run RTT samples in ns alongside unchanged integer-us CSV")
    parser.add_argument("--request-traces", action="store_true", help="requires diagnostic-enabled clients")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("this controlled runner currently requires Linux")
    if any(n <= 0 for n in [*args.payloads, args.abba_rounds, args.duration, args.iterations, args.timeout]):
        parser.error("sizes, rounds, duration, iterations and timeout must be positive")
    if args.warmup < 0 or args.udp_max_payload < 0:
        parser.error("warmup and UDP cap must not be negative")
    if len(set(args.payloads)) != len(args.payloads) or len(set(args.transports)) != len(args.transports):
        parser.error("duplicate payloads/transports would reuse output files")
    if os.geteuid() == 0:
        parser.error("run benchmarks as an ordinary user, including under the clock controller")
    if any(cpu is not None and cpu < 0 for cpu in [args.server_cpu, args.client_cpu, args.matrix_main_cpu]):
        parser.error("CPU indices must not be negative")
    if args.matrix_worker_cpus and any(not x.isdigit() for x in args.matrix_worker_cpus.split(",")):
        parser.error("worker CPUs must be comma-separated nonnegative integers")
    args.output = args.output.resolve()
    builds = dict(zip(LABELS, [args.baseline_build.resolve(), args.candidate_build.resolve()]))
    if args.matrix_preload:
        args.matrix_preload = args.matrix_preload.resolve(strict=True)
        if args.matrix_main_cpu is None or not args.matrix_worker_cpus:
            parser.error("preload requires --matrix-main-cpu and --matrix-worker-cpus")
    required = set()
    if args.suite != "latency":
        required.add("bench_strategy_matrix")
    if args.suite != "matrix":
        for transport in args.transports:
            required.update([f"bench_{transport}_echo_server", f"bench_{transport}_latency_client"])
    for build in builds.values():
        for name in required:
            if not os.access(build / "bin" / name, os.X_OK):
                parser.error(f"missing executable: {build / 'bin' / name}")
    args.output.mkdir(parents=True, exist_ok=False)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    for sig in [signal.SIGTERM, signal.SIGHUP]:
        signal.signal(sig, interrupted)
    metadata = {
        "schema": 1, "benchmark_git": git_state(Path(__file__).resolve().parents[1]), "options": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "builds": {label: manifest(build) for label, build in builds.items()},
        "environment": collect_environment(),
        "preload_sha256": digest(args.matrix_preload) if args.matrix_preload else None,
    }
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    completed = []
    try:
        for size in args.payloads:
            for index, label in enumerate(["baseline", "candidate", "candidate", "baseline"] * args.abba_rounds):
                build = builds[label]
                if args.suite != "latency":
                    name = f"matrix-{size}-{index}-{label}"
                    prefix = args.output / name
                    env = dict(os.environ)
                    env.pop("LD_PRELOAD", None)
                    if args.matrix_preload:
                        env["LD_PRELOAD"] = str(args.matrix_preload)
                        env["WIRESTEAD_BENCH_MAIN_CPU"] = str(args.matrix_main_cpu)
                        env["WIRESTEAD_BENCH_WORKER_CPUS"] = args.matrix_worker_cpus
                    execute(command(build / "bin/bench_strategy_matrix", args.matrix_cpus)
                            + ["--payload-size", str(size), "--duration", str(args.duration),
                               "--csv-output", str(prefix.with_suffix(".csv"))], prefix, args.timeout, env)
                    result = rows(prefix.with_suffix(".csv"))
                    if {(x["transport"], x["strategy"]) for x in result} != {
                            (t, s) for t in ["tcp", "udp", "uds"] for s in ["reliable", "besteffort"]} or len(result) != 6:
                        raise ValueError("matrix output must contain six distinct phases")
                    completed.append(name)
                if args.suite != "matrix":
                    for transport in args.transports:
                        if transport == "udp" and args.udp_max_payload and size > args.udp_max_payload:
                            continue
                        name = f"latency-{transport}-{size}-{index}-{label}"
                        latency(args, build, name, transport, size)
                        completed.append(name)
                print(f"completed {size} {index} {label}", flush=True)
        for build in metadata["builds"].values():
            for path, expected in build["files"].items():
                if digest(Path(path)) != expected:
                    raise RuntimeError(f"binary changed during measurement: {path}")
        status = {"status": "success", "completed": completed,
                  "sha256": {p.name: digest(p) for p in args.output.iterdir() if p.is_file()}}
    except BaseException as error:
        (args.output / "completed.json").write_text(json.dumps(
            {"status": "failed", "completed": completed, "error": repr(error)}, indent=2))
        raise
    (args.output / "completed.json").write_text(json.dumps(status, indent=2) + "\n")


if __name__ == "__main__":
    main()
