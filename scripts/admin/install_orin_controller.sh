#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo "Run this installer with sudo" >&2; exit 1; }
id jorin >/dev/null
# Keep installation out of any active manual/CI controller interval.
if [[ "${1:-}" != --lock-held ]]; then
  exec flock -n /run/lock/wirestead-benchmark-cpufreq.lock /bin/bash "$0" --lock-held
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
controller=/usr/local/libexec/wirestead-clock-run.py
policy=/etc/sudoers.d/wirestead-benchmark
for path in /usr/local/libexec /var/log/wirestead-benchmark; do
  [[ ! -L "$path" ]] || { echo "Refusing symlink: $path" >&2; exit 1; }
  install -d -o root -g root -m 0755 "$path"
done
[[ ! -L "$controller" && ! -L "$policy" ]] || { echo "Refusing symlink destination" >&2; exit 1; }
# Exactly one isolated Python entry point; its command arguments run as jorin.
staged=$(mktemp /etc/sudoers.d/.wirestead-benchmark.XXXXXX)
trap 'rm -f -- "$staged"' EXIT
printf '%s\n' 'jorin ALL=(root) NOPASSWD: /usr/bin/python3 -I /usr/local/libexec/wirestead-clock-run.py *' > "$staged"
chmod 0440 "$staged"
visudo -cf "$staged"
install -o root -g root -m 0755 "$source_dir/clock_run.py" "$controller"
install -o root -g root -m 0440 "$staged" "$policy"
echo 'Installed. As jorin, check with: sudo -n /usr/bin/python3 -I /usr/local/libexec/wirestead-clock-run.py --check'
