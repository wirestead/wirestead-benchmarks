#!/usr/bin/python3
"""Installed, root-owned Orin clock controller. Never import checkout code."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import time
import uuid

USER = "jorin"
LOG_ROOT = Path("/var/log/wirestead-benchmark")
LOCK = Path("/run/lock/wirestead-benchmark-cpufreq.lock")


def secure_directory(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f"not a root-owned, non-writable directory: {part}")


def snapshot(policies):
    return {str(p): {key: (p / key).read_text().strip()
                    for key in ("scaling_min_freq", "scaling_max_freq", "scaling_governor")}
            for p in policies}


def minimum(policy, value):
    (policy / "scaling_min_freq").write_text(value)
    deadline = time.monotonic() + 5
    while (policy / "scaling_min_freq").read_text().strip() != value:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"clock readback mismatch: {policy}")
        time.sleep(.05)


def fixed_clocks(policies, action, record, write_minimum=minimum, before_restore=lambda: None):
    before = snapshot(policies)
    record("clock-before.json", before)
    changed = []
    try:
        for policy in policies:
            changed.append(policy)
            write_minimum(policy, before[str(policy)]["scaling_max_freq"])
        return action()
    finally:
        before_restore()
        errors = []
        for policy in reversed(changed):
            try:
                write_minimum(policy, before[str(policy)]["scaling_min_freq"])
            except Exception as error:
                errors.append(f"{policy}: {error}")
        after = snapshot(policies)
        record("clock-after.json", {"policies": after, "restore_errors": errors})
        if errors or after != before:
            raise RuntimeError("clock restoration failed; inspect controller evidence")


def stop(proc):
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def command_for(account, command, label, timeout):
    # systemd owns the entire descendant cgroup, including benchmark children
    # that start their own process groups. Only this service runs user code.
    return ["/usr/bin/systemd-run", "--quiet", "--wait", "--pipe", "--collect",
            "--service-type=exec", "--unit=wirestead-benchmark-" + label,
            "--uid=" + USER, "--gid=" + str(account.pw_gid),
            "--property=KillMode=control-group", "--property=TimeoutStopSec=5s",
            "--property=RuntimeMaxSec=" + str(timeout) + "s",
            "--working-directory=" + os.getcwd(),
            "/usr/bin/env", "-i", "HOME=" + account.pw_dir, "USER=" + USER,
            "LOGNAME=" + USER, "PATH=/usr/local/bin:/usr/bin:/bin", *command]


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--label", default="manual-" + uuid.uuid4().hex)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 7200:
        parser.error("timeout must be between 1 and 7200 seconds")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.label):
        parser.error("invalid evidence label")
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.check and (not args.command or not args.command[0].startswith("/")):
        parser.error("provide an absolute executable after --")
    return args


def wait_or_orphaned(proc, timeout, parent, poll=1.0):
    # sudo neither relays INT/TERM/HUP here nor takes this process with it when
    # it is killed, which is how a runner cancels a job. Treat losing the sudo
    # parent as an interrupt so the service stops and clocks are restored now.
    deadline = time.monotonic() + timeout
    while True:
        try:
            return proc.wait(timeout=max(0, min(poll, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            if os.getppid() != parent:
                raise KeyboardInterrupt("parent sudo exited")
            if time.monotonic() >= deadline:
                raise


def main():
    parent = os.getppid()
    args = parse()
    account = pwd.getpwnam(USER)
    if os.geteuid() != 0 or account.pw_uid == 0 or os.environ.get("SUDO_UID") != str(account.pw_uid):
        raise SystemExit("invoke through sudo as jorin")
    secure_directory(Path(__file__).parent)
    script = Path(__file__).lstat()
    if not stat.S_ISREG(script.st_mode) or script.st_uid != 0 or script.st_mode & 0o022:
        raise SystemExit("controller must be a root-owned regular file")
    secure_directory(LOG_ROOT)
    policies = sorted(Path("/sys/devices/system/cpu/cpufreq").glob("policy*"))
    if not policies:
        raise SystemExit("no CPU frequency policies")
    # /run/lock may be sticky/world-writable; reject a pre-created user-owned
    # or symlink lock before touching it. Use the legacy controller's lock.
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("unsafe clock lock file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.check:
            print(json.dumps({"user": USER, "policies": snapshot(policies), "available": True}))
            return
        out = LOG_ROOT / args.label
        out.mkdir(mode=0o755)
        os.chmod(out, 0o755)
        def record(name, data):
            path = out / name
            path.write_text(json.dumps(data, indent=2) + "\n")
            os.chmod(path, 0o644)
        def ignore_signals():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)
        def interrupted(signum, frame):
            ignore_signals()
            raise KeyboardInterrupt(f"signal {signum}")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, interrupted)
        def action():
            proc = None
            try:
                proc = subprocess.Popen(command_for(account, args.command, args.label, args.timeout), start_new_session=True)
                return wait_or_orphaned(proc, args.timeout + 15, parent)
            finally:
                ignore_signals()
                unit = "wirestead-benchmark-" + args.label + ".service"
                try:
                    stopped = subprocess.run(["/usr/bin/systemctl", "stop", unit], capture_output=True, timeout=15)
                    if stopped.returncode:
                        state = subprocess.run(["/usr/bin/systemctl", "show", "-p", "LoadState", "--value", unit], capture_output=True, text=True, timeout=10).stdout.strip()
                        if state != "not-found":
                            raise RuntimeError("failed to stop benchmark cgroup")
                finally:
                    stop(proc)
        code = 1
        try:
            code = fixed_clocks(policies, action, record, before_restore=ignore_signals)
            record("result.json", {"returncode": code})
        except BaseException as error:
            record("result.json", {"returncode": code, "error": repr(error)})
            raise
        finally:
            print("Controller evidence: " + str(out), flush=True)
        raise SystemExit(code)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
