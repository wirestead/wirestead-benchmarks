import csv
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LatencySamplesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('c++')
        if not compiler:
            raise unittest.SkipTest('C++ compiler required for the latency runner probe')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        source = cls.root / 'probe.cpp'
        source.write_text(r'''
#include "common/latency_runner.hpp"
struct EchoClient {
  wirestead_bench::EchoWaiter waiter;
  bool start_sync() { return true; }
  bool send_frame(std::string_view frame) { waiter.on_bytes(frame); return true; }
  void stop() {}
  wirestead_bench::EchoWaiter& echo_waiter() { return waiter; }
};
int main(int argc, char** argv) {
  try {
    EchoClient client;
    return wirestead_bench::run_latency_client("fake", client, 16, 101, 7, argv[1]);
  } catch (const std::exception&) { return 2; }
}
''')
        cls.exe = cls.root / 'probe'
        subprocess.run([compiler, '-std=c++20', '-pthread', '-I', str(ROOT / 'benchmarks'),
                        str(source), '-o', str(cls.exe)], check=True, capture_output=True)

    def run_probe(self, directory, sample_path=None):
        env = dict(os.environ)
        for name in ['WIRESTEAD_REQUEST_TRACE', 'WIRESTEAD_LATENCY_SAMPLES', 'OUTLIER_THRESHOLDS_US']:
            env.pop(name, None)
        if sample_path is not None:
            env['WIRESTEAD_LATENCY_SAMPLES'] = str(sample_path)
        return subprocess.run([str(self.exe), str(directory / 'summary.csv')], env=env,
                              capture_output=True, text=True)

    def test_raw_samples_reproduce_legacy_percentiles_and_exclude_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / 'samples.csv'
            result = self.run_probe(root, raw)
            self.assertEqual(result.returncode, 0, result.stderr)
            with raw.open() as stream:
                samples = list(csv.DictReader(stream))
            self.assertEqual([int(x['iteration']) for x in samples], list(range(101)))
            values = sorted(int(x['rtt_ns']) for x in samples)
            self.assertTrue(all(x >= 0 for x in values))
            with (root / 'summary.csv').open() as stream:
                summary = next(csv.DictReader(stream))
            for field, pct in [('p50_us', 50), ('p95_us', 95), ('p99_us', 99), ('p99_9_us', 99.9)]:
                self.assertEqual(int(summary[field]), values[int(pct / 100 * 100)] // 1000)
            self.assertEqual(int(summary['iterations']), 101)
            self.assertEqual(int(summary['warmup_iterations']), 7)
            self.assertAlmostEqual(float(summary['avg_us']), sum(x // 1000 for x in values) / 101, places=5)

    def test_disabled_recording_keeps_standard_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.run_probe(root).returncode, 0)
            self.assertEqual({x.name for x in root.iterdir()}, {'summary.csv'})

    def test_unwritable_sample_output_fails_without_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertNotEqual(self.run_probe(root, root / 'missing' / 'samples.csv').returncode, 0)
            self.assertFalse((root / 'summary.csv').exists())


if __name__ == '__main__':
    unittest.main()
