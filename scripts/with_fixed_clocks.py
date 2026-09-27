#!/usr/bin/env python3
"""Temporarily fix Linux CPU minima to current maxima; run a command as a non-root user."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import signal
import subprocess
import time

from compare_builds import stop


def snapshot(policies):
    return {str(p): {n: (p / n).read_text().strip()
                    for n in ("scaling_min_freq", "scaling_max_freq", "scaling_governor")}
            for p in policies}


def set_minimum(policy, value, timeout=5):
    path = policy / "scaling_min_freq"
    path.write_text(value)
    deadline = time.monotonic() + timeout
    while path.read_text().strip() != value:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"frequency readback mismatch: {policy}")
        time.sleep(.05)


def restore(changed, before):
    errors = []
    for policy in reversed(changed):
        try:
            set_minimum(policy, before[str(policy)]["scaling_min_freq"])
        except Exception as error:
            errors.append(f"{policy}: {error}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="ordinary benchmark user")
    parser.add_argument("--log-dir", required=True, type=Path, help="new controller evidence directory")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--perf", type=Path, help="optional perf executable for system-wide scheduler tracing")
    parser.add_argument("--check", action="store_true", help="read-only preflight")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd or args.timeout <= 0:
        parser.error("provide a command after -- and a positive timeout")
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        parser.error("benchmark user must not be root")
    policies = sorted(Path("/sys/devices/system/cpu/cpufreq").glob("policy*"))
    if not policies:
        parser.error("no CPU frequency policies found")
    before = snapshot(policies)
    if args.check:
        print(json.dumps({"before": before, "command": cmd, "user": args.user}, indent=2))
        return
    if os.geteuid() != 0:
        parser.error("clock changes require sudo; benchmark command runs as --user")
    events = ["sched_switch", "sched_wakeup", "sched_wakeup_new", "sched_migrate_task"]
    if args.perf:
        args.perf = args.perf.resolve(strict=True)
        for event in events:
            if not any((Path(base) / "events/sched" / event / "id").exists()
                       for base in ["/sys/kernel/tracing", "/sys/kernel/debug/tracing"]):
                parser.error(f"required scheduler tracepoint unavailable: {event}")
    # Serialize this controller's clock mutations across concurrent invocations.
    with open("/run/lock/wirestead-benchmark-cpufreq.lock", "w") as clock_lock:
        fcntl.flock(clock_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = snapshot(policies)
        out = args.log_dir.resolve()
        out.mkdir(parents=True, exist_ok=False)
        os.chown(out, account.pw_uid, account.pw_gid)
        (out / "clock-before.json").write_text(json.dumps(before, indent=2))
        def interrupted(signum, frame):
            raise KeyboardInterrupt(f"signal {signum}")
        for sig in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
            signal.signal(sig, interrupted)
        changed, errors = [], []
        proc = None
        code = 1
        try:
            for policy in policies:
                changed.append(policy)  # Include writes that fail after mutation.
                set_minimum(policy, before[str(policy)]["scaling_max_freq"])
            command = ["runuser", "-u", args.user, "--", *cmd]
            if args.perf:
                prefix = [str(args.perf), "record", "-a", "-k", "mono", "-m", "1024",
                          "-o", str(out / "sched.data")]
                for event in events:
                    prefix += ["-e", "sched:" + event]
                command = prefix + ["--", *command]
            with (out / "command.log").open("w") as log:
                proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                code = proc.wait(timeout=args.timeout)
        finally:
            for sig in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
                signal.signal(sig, signal.SIG_IGN)
            try:
                stop(proc)
            finally:
                errors = restore(changed, before)
                after = snapshot(policies)
                (out / "clock-after.json").write_text(json.dumps(
                    {"policies": after, "restore_errors": errors}, indent=2))
                for path in out.iterdir():
                    if path.is_file():
                        os.chown(path, account.pw_uid, account.pw_gid)
        if errors or after != before:
            raise RuntimeError("CPU restoration failed; inspect clock-after.json")
        if args.perf and (out / "sched.data").exists():
            # Decode as the data owner, not root, after restoring clocks.
            with (out / "sched.txt").open("w") as output:
                decoded = subprocess.run(["runuser", "-u", args.user, "--", str(args.perf),
                                          "script", "-i", str(out / "sched.data"), "--ns"],
                                         stdout=output, stderr=subprocess.PIPE, text=True)
            (out / "decode.json").write_text(json.dumps(
                {"returncode": decoded.returncode, "stderr": decoded.stderr}, indent=2))
            for name in ["sched.txt", "decode.json"]:
                os.chown(out / name, account.pw_uid, account.pw_gid)
            if decoded.returncode:
                raise RuntimeError("scheduler decoding failed; raw capture retained")
        raise SystemExit(code)


if __name__ == "__main__":
    main()
