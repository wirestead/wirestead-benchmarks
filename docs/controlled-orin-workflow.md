# Controlled Orin workflow

The manually dispatched Controlled Orin Comparison workflow builds two core refs
using one benchmark checkout, then runs six trials per version/condition.
It uploads evidence only and never publishes a release. Core refs must be
available in wirestead/wirestead; local unpublished commits cannot be selected.

The matrix covers TCP/UDP/UDS, Reliable/BestEffort, 64B/1KiB/4KiB, split/shared CPU
layouts. Echo latency uses the existing standard clients (Reliable); this does
not yet include the separate experimental BestEffort latency fixture. Therefore
this workflow alone is not the full release gate.

## One-time administrator installation

Review scripts/admin/clock_run.py and install_orin_controller.sh first. On Orin:

~~~sh
sudo bash /path/to/wirestead-benchmarks/scripts/admin/install_orin_controller.sh
sudo -n /usr/bin/python3 -I /usr/local/libexec/wirestead-clock-run.py --check
~~~

The installer targets the existing account jorin and installs a root-owned
controller at /usr/local/libexec/wirestead-clock-run.py. sudoers allows only the
isolated Python interpreter (-I) with this specific script. Do not allow
passwordless execution of checkout scripts, generic Python, shell, or all sudo.

Only the controller changes CPU minimum frequencies to the current maximum.
It has no user-selectable sysfs, log, controller or perf path.
The arbitrary child command is executed by a transient systemd service as jorin,
with a clean environment. All descendants, including new process groups, remain
in that service cgroup. Time limit is at most two hours, stop timeout five seconds.
The controller stops that service before restoring clocks on normal completion,
error, timeout, and when its sudo parent exits. INT/TERM/HUP sent to sudo are not
relayed to the controller, and a cancelled Actions job ends with the runner
killing sudo, so the controller polls for that once a second and treats it as an
interrupt. Signals sent to the controller itself need root. SIGKILL of the controller, kernel failure,
power loss or a broken sysfs driver cannot be guaranteed recoverable automatically;
inspect /var/lib/wirestead-benchmark if a run terminates abnormally.

Root-owned, read-only evidence is under /var/lib/wirestead-benchmark/LABEL.
No files under a user-provided output directory are opened by root.
The shared lock /run/lock/wirestead-benchmark-cpufreq.lock also excludes the
existing manual controller. A busy device fails preflight instead of joining
an active measurement.

The account must match the self-hosted runner. The runner needs systemd,
Python3, CMake, a C compiler, and the existing arm64 vcpkg toolchain at
/home/jorin/workspace/unilink-lab/vcpkg/scripts/buildsystems/vcpkg.cmake.
Do not install/update the controller while it is running.

## Using the workflow

Select baseline_ref and candidate_ref in Actions. The workflow records resolved
SHAs; both versions use the same Release toolchain and benchmark source. Builds
run under the same controller lock as measurement to avoid interference with a
manual benchmark, followed by a short settling interval.

The legacy Benchmark Release and Load Sweep workflows share the same repository
concurrency group with this workflow. This conservatively serializes all those
self-hosted workflow runs, including other platforms. Unrelated repositories or
external jobs do not participate and must not run on Orin during measurement.

After installation, the controller also works via SSH without GitHub Actions:

~~~sh
sudo -n /usr/bin/python3 -I /usr/local/libexec/wirestead-clock-run.py --timeout 3600 -- /usr/bin/python3 /absolute/path/to/user-comparison.py
~~~

That script must execute its measurement directly as jorin, not call sudo again.
The older run_* wrappers requiring root are not directly compatible; use their
ordinary-user measurement entry point or the new workflow script.

## Removing permission

An administrator may remove /etc/sudoers.d/wirestead-benchmark and the installed
controller once no run is active. Existing evidence remains available.
