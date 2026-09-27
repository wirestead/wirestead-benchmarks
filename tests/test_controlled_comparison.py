import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compare_builds as runner
import summarize_comparison as summary
import with_fixed_clocks as clocks


class TelemetryTests(unittest.TestCase):
    def test_transient_sysfs_read_is_missing_not_fatal(self):
        source = Mock()
        source.read_bytes.side_effect = [None, BlockingIOError(), b"55312\n"]
        self.assertIsNone(runner.read(source))
        self.assertIsNone(runner.read(source))
        self.assertEqual(runner.read(source), "55312")

    def test_unreadable_sensor_is_not_a_measurement(self):
        source = Mock()
        source.read_bytes.side_effect = [PermissionError(), b"\xff"]
        self.assertIsNone(runner.read(source))
        self.assertIsNone(runner.read(source))


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        options = dict(payloads=[1024], suite="latency", transports=["tcp"],
                       udp_max_payload=1024, abba_rounds=1, iterations=100, warmup=10)
        (self.root / "metadata.json").write_text(json.dumps({"options": options}))
        self.names = []
        for i, label in enumerate(["baseline", "candidate", "candidate", "baseline"]):
            name = f"latency-tcp-1024-{i}-{label}"
            self.names.append(name)
            with (self.root / (name + ".csv")).open("w") as f:
                writer = csv.DictWriter(f, fieldnames=["transport", "payload_size", "p50_us", "p99_us", "iterations", "warmup_iterations"])
                writer.writeheader()
                writer.writerow(dict(transport="tcp", payload_size=1024, p50_us=10,
                                     p99_us=[20, 18, 100, 20][i], iterations=100, warmup_iterations=10))
        self.status("success", self.names)

    def status(self, state, names):
        (self.root / "completed.json").write_text(json.dumps({"status": state, "completed": names, "sha256": {p.name: runner.digest(p) for p in self.root.iterdir() if p.name != "completed.json"}}))

    def test_outlier_is_retained_not_filtered(self):
        result, _ = summary.summarize(self.root)
        metric = result[0]["metrics"]["p99_us"]
        self.assertEqual(metric["versions"]["candidate"]["max"], 100)
        self.assertEqual(metric["versions"]["candidate"]["median"], 59)
        self.assertAlmostEqual(metric["change_pct"], 195)

    def test_failed_or_missing_trial_is_not_a_comparison(self):
        for state, names in [("failed", self.names), ("success", self.names[:-1]), ("success", [])]:
            self.status(state, names)
            with self.assertRaises(ValueError):
                summary.summarize(self.root)

    def test_modified_result_rejected(self):
        f = self.root / (self.names[0] + ".csv")
        f.write_text(f.read_text().replace(",20", ",21"))
        with self.assertRaises(ValueError):
            summary.summarize(self.root)

    def test_nonfinite_metric_rejected(self):
        f = self.root / (self.names[0] + ".csv")
        f.write_text(f.read_text().replace(",20", ",nan"))
        self.status("success", self.names)
        with self.assertRaises(ValueError):
            summary.summarize(self.root)


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policies = []
        for n in ["policy0", "policy4"]:
            p = self.root / "cpu" / n
            p.mkdir(parents=True)
            for name, value in [("scaling_min_freq", "100"), ("scaling_max_freq", "200"),
                                ("scaling_governor", "schedutil")]:
                (p / name).write_text(value)
            self.policies.append(p)
        self.before = clocks.snapshot(self.policies)

    def run_controller(self, failure=None, write_failure=False):
        log = self.root / "logs"
        args = ["clock", "--user", "bench", "--log-dir", str(log), "--", "benchmark"]
        proc = types.SimpleNamespace(pid=12345)
        def wait(timeout):
            if failure:
                raise failure
            return 0
        proc.wait = wait
        actual_path = Path
        def path(value):
            return self.root / "cpu" if str(value) == "/sys/devices/system/cpu/cpufreq" else actual_path(value)
        original = clocks.set_minimum
        calls = []
        def setter(policy, value):
            original(policy, value)
            calls.append((policy, value))
            if write_failure and len(calls) == 2:
                raise OSError("failure after a partial mutation")
        with patch.object(sys, "argv", args), patch.object(clocks, "Path", side_effect=path), \
                patch.object(clocks.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=1000, pw_gid=1000)), \
                patch.object(clocks.os, "geteuid", return_value=0), patch.object(clocks.os, "chown"), \
                patch.object(clocks, "open", return_value=(self.root / "lock").open("w"), create=True), \
                patch.object(clocks.fcntl, "flock"), patch.object(clocks.signal, "signal"), \
                patch.object(clocks.subprocess, "Popen", return_value=proc), \
                patch.object(clocks, "stop") as stopped, patch.object(clocks, "set_minimum", side_effect=setter):
            expected = type(failure) if failure else OSError if write_failure else SystemExit
            with self.assertRaises(expected):
                clocks.main()
            self.assertEqual(clocks.snapshot(self.policies), self.before)
            after = json.loads((log / "clock-after.json").read_text())
            self.assertEqual(after["restore_errors"], [])
            self.assertTrue(stopped.called)

    def test_delayed_readback_is_polled(self):
        fake = Mock()
        target = Mock()
        fake.__truediv__ = Mock(return_value=target)
        target.read_text.side_effect = ["200", "200", "100"]
        with patch.object(clocks.time, "sleep"):
            clocks.set_minimum(fake, "100")
        target.write_text.assert_called_once_with("100")
        self.assertEqual(target.read_text.call_count, 3)

    def test_restore_continues_after_one_policy_fails(self):
        for policy in self.policies:
            (policy / "scaling_min_freq").write_text("200")
        original = clocks.set_minimum
        def setter(policy, value):
            if policy == self.policies[1]:
                raise OSError("persistent readback failure")
            original(policy, value)
        with patch.object(clocks, "set_minimum", side_effect=setter):
            errors = clocks.restore(self.policies, self.before)
        self.assertEqual(len(errors), 1)
        self.assertEqual((self.policies[0] / "scaling_min_freq").read_text(), "100")

    def test_success_restores(self):
        self.run_controller()

    def test_timeout_restores(self):
        self.run_controller(subprocess.TimeoutExpired("benchmark", 1))

    def test_interrupt_restores(self):
        self.run_controller(KeyboardInterrupt())

    def test_partial_write_failure_restores_all_changed_policies(self):
        self.run_controller(write_failure=True)


class ProcessTests(unittest.TestCase):
    def test_timeout_terminates_private_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            pidfile = path / "pid"
            code = "import os,time;open(" + repr(str(pidfile)) + ",'w').write(str(os.getpid()));time.sleep(30)"
            with self.assertRaises(TimeoutError):
                runner.execute([sys.executable, "-c", code], path / "run", .2)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)


if __name__ == "__main__":
    unittest.main()
