import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

path = pathlib.Path(__file__).parents[1] / "scripts/admin/clock_run.py"
spec = importlib.util.spec_from_file_location("installed_clock_controller", path)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)

class ControllerTest(unittest.TestCase):
    def policies(self, root):
        policies = [root / "policy0", root / "policy4"]
        for p in policies:
            p.mkdir()
            for name, value in [("scaling_min_freq", "100"), ("scaling_max_freq", "200"),
                                ("scaling_governor", "schedutil")]:
                (p / name).write_text(value)
        return policies

    def exercise(self, action):
        with tempfile.TemporaryDirectory() as temp:
            policies = self.policies(pathlib.Path(temp))
            before = controller.snapshot(policies)
            records = {}
            def wrapped():
                self.assertTrue(all((p / "scaling_min_freq").read_text() == "200" for p in policies))
                return action()
            try:
                return controller.fixed_clocks(policies, wrapped, records.__setitem__)
            finally:
                self.assertEqual(controller.snapshot(policies), before)
                self.assertEqual(records["clock-after.json"]["restore_errors"], [])

    def test_success_and_child_failure_restore(self):
        self.assertEqual(self.exercise(lambda: 0), 0)
        self.assertEqual(self.exercise(lambda: 7), 7)

    def test_timeout_and_signal_restore(self):
        for error in [subprocess.TimeoutExpired("command", 1), KeyboardInterrupt("signal")]:
            def fail():
                raise error
            with self.assertRaises(type(error)):
                self.exercise(fail)

    def test_partial_clock_mutation_restores(self):
        with tempfile.TemporaryDirectory() as temp:
            policies = self.policies(pathlib.Path(temp))
            before = controller.snapshot(policies)
            records = {}
            def fail_second(policy, value):
                controller.minimum(policy, value)
                if policy == policies[1] and value == "200":
                    raise OSError("failed after mutation")
            with self.assertRaises(OSError):
                controller.fixed_clocks(policies, lambda: self.fail("must not start"), records.__setitem__, fail_second)
            self.assertEqual(controller.snapshot(policies), before)

    def test_restore_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as temp:
            policies = self.policies(pathlib.Path(temp))
            records = {}
            def fail_restore(policy, value):
                if value == "100":
                    raise OSError("restore denied")
                controller.minimum(policy, value)
            with self.assertRaisesRegex(RuntimeError, "restoration failed"):
                controller.fixed_clocks(policies, lambda: 0, records.__setitem__, fail_restore)
            self.assertEqual(len(records["clock-after.json"]["restore_errors"]), 2)

    def test_parser_rejects_paths_and_unbounded_time(self):
        for args in [["--label", "../escape", "--check"], ["--timeout", "7201", "--check"],
                     ["--", "python3", "script.py"], ["--user", "root", "--check"]]:
            with self.assertRaises(SystemExit):
                controller.parse(args)

    def test_commands_are_ordinary_user_cgroup_with_clean_environment(self):
        account = mock.Mock(pw_dir="/home/jorin", pw_gid=1000)
        args = controller.command_for(account, ["/bin/bash", "-c", "echo ok"], "unit-1", 30)
        self.assertIn("--uid=jorin", args)
        self.assertIn("--gid=1000", args)
        self.assertIn("--property=KillMode=control-group", args)
        self.assertIn("--property=RuntimeMaxSec=30s", args)
        self.assertEqual(args[args.index("/usr/bin/env")+1], "-i")
        self.assertEqual(args[-3:], ["/bin/bash", "-c", "echo ok"])

    def test_parser_keeps_user_command_opaque(self):
        args = controller.parse(["--label", "ci-17", "--", "/bin/bash", "-c",
                                 "echo --user=root; true"])
        self.assertEqual(args.command, ["/bin/bash", "-c", "echo --user=root; true"])

    def test_descendant_service_timeout_is_bounded(self):
        account = mock.Mock(pw_dir="/home/jorin", pw_gid=1000)
        command = controller.command_for(account, ["/usr/bin/true"], "safe", 60)
        self.assertIn("--property=TimeoutStopSec=5s", command)
        self.assertIn("--unit=wirestead-benchmark-safe", command)
        self.assertNotIn("--uid=root", command)

    def test_directory_check_rejects_user_owned_or_writable_path(self):
        with tempfile.TemporaryDirectory() as temp:
            p = pathlib.Path(temp)
            p.chmod(0o777)
            with self.assertRaises(RuntimeError):
                controller.secure_directory(p)

    def test_restore_hook_precedes_restoring_clocks(self):
        with tempfile.TemporaryDirectory() as temp:
            policies = self.policies(pathlib.Path(temp))
            restoring = []
            def write(p, v):
                if v == "100":
                    self.assertEqual(restoring, [True])
                controller.minimum(p, v)
            controller.fixed_clocks(policies, lambda: 0, lambda *args: None,
                                    write, lambda: restoring.append(True))

if __name__ == "__main__":
    unittest.main()
