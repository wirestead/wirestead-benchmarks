#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT_DIR}/scripts/bench_common.sh"
WIRESTEAD_BENCH_BUILD_DIR="${WIRESTEAD_BENCH_BUILD_DIR:-${UNILINK_BENCH_BUILD_DIR:-${ROOT_DIR}/build}}"
BIN_DIR="${WIRESTEAD_BENCH_BUILD_DIR}/bin"

PORT="${PORT:-9100}"
CONNECTIONS="${CONNECTIONS:-32}"
RATE_PER_CONNECTION="${RATE_PER_CONNECTION:-500}"
DURATION_MS="${DURATION_MS:-5000}"
PAYLOAD_SIZE="${PAYLOAD_SIZE:-256}"
BURST="${BURST:-1}"

# Server and clients share one process here, so unlike the other latency
# scripts there is nothing to spawn and wait for.
"${BIN_DIR}/bench_tcp_load_latency" \
  --port "${PORT}" \
  --connections "${CONNECTIONS}" \
  --rate-per-connection "${RATE_PER_CONNECTION}" \
  --duration-ms "${DURATION_MS}" \
  --payload-size "${PAYLOAD_SIZE}" \
  --burst "${BURST}" \
  "$@"
