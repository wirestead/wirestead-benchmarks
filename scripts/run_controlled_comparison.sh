#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) != 0 ]] || { echo "Measurements must not run as root" >&2; exit 1; }
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"
output="${1:?provide a new output directory}"
[[ ! -e "$output" ]] || { echo "Output already exists" >&2; exit 1; }
mkdir -p "$output"
toolchain=/home/jorin/workspace/unilink-lab/vcpkg/scripts/buildsystems/vcpkg.cmake
[[ -f "$toolchain" ]]
for variant in baseline candidate; do
  cmake -S . -B "$output/build-$variant"     -DCMAKE_BUILD_TYPE=Release -DCMAKE_TOOLCHAIN_FILE="$toolchain"     -DVCPKG_TARGET_TRIPLET=arm64-linux -DWIRESTEAD_BENCH_USE_FETCHCONTENT=ON     -DWIRESTEAD_BENCH_SOURCE_DIR="$repo/_cores/$variant"
  cmake --build "$output/build-$variant" -j2
done
cc -shared -fPIC -O2 scripts/diagnostics/thread_affinity.c -o "$output/thread_affinity.so" -ldl -pthread
# Let compilation activity settle before the serial comparison.
sleep 20
for layout in split shared; do
  cpus=1,2,3,4
  [[ "$layout" != shared ]] || cpus=1,2,0,3
  python3 scripts/compare_builds.py --baseline-build "$output/build-baseline"     --candidate-build "$output/build-candidate" --output "$output/$layout"     --suite matrix --payloads 64 1024 4096 --duration 3 --abba-rounds 3     --matrix-preload "$output/thread_affinity.so" --matrix-main-cpu 0 --matrix-worker-cpus "$cpus"
  python3 scripts/summarize_comparison.py "$output/$layout"
done
python3 scripts/compare_builds.py --baseline-build "$output/build-baseline"   --candidate-build "$output/build-candidate" --output "$output/latency"   --suite latency --transports tcp udp uds --payloads 64 1024 4096   --abba-rounds 3 --server-cpu 1 --client-cpu 2
python3 scripts/summarize_comparison.py "$output/latency"
