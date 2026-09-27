#!/usr/bin/env python3
"""Validate and summarize one completed compare_builds.py run."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics

from compare_builds import digest


def summarize(root):
    status = json.loads((root / "completed.json").read_text())
    if status["status"] != "success":
        raise ValueError("refusing an incomplete/failed comparison")
    if not status.get("sha256"):
        raise ValueError("missing result manifest")
    for name, expected_hash in status["sha256"].items():
        if Path(name).name != name or digest(root / name) != expected_hash:
            raise ValueError("result manifest mismatch: " + name)
    meta = json.loads((root / "metadata.json").read_text())
    options = meta["options"]
    expected = set()
    for size in options["payloads"]:
        for index, label in enumerate(["baseline", "candidate", "candidate", "baseline"] * options["abba_rounds"]):
            if options["suite"] != "latency":
                expected.add(f"matrix-{size}-{index}-{label}")
            if options["suite"] != "matrix":
                for transport in options["transports"]:
                    if transport == "udp" and options["udp_max_payload"] and size > options["udp_max_payload"]:
                        continue
                    expected.add(f"latency-{transport}-{size}-{index}-{label}")
    if not expected or set(status["completed"]) != expected or len(status["completed"]) != len(expected):
        raise ValueError("completed trial list does not match planned comparison")
    groups = {}
    for name in status["completed"]:
        with (root / (name + ".csv")).open() as stream:
            rows = list(csv.DictReader(stream))
        if not rows or any(None in row.values() for row in rows):
            raise ValueError("empty or truncated CSV: " + name)
        kind = name.split("-", 1)[0]
        if kind == "matrix":
            if len(rows) != 6 or {(x["transport"], x["strategy"]) for x in rows} != {
                    (t, s) for t in ["tcp", "udp", "uds"] for s in ["reliable", "besteffort"]}:
                raise ValueError("incomplete strategy phases: " + name)
        elif len(rows) != 1:
            raise ValueError("latency row count mismatch: " + name)
        parts = name.split("-")
        expected_size = int(parts[1] if kind == "matrix" else parts[2])
        for row in rows:
            if int(row["payload_size"]) != expected_size:
                raise ValueError("payload mismatch: " + name)
            if kind == "latency" and (row["transport"] != parts[1]
                    or int(row["iterations"]) != options["iterations"]
                    or int(row["warmup_iterations"]) != options["warmup"]):
                raise ValueError("latency settings mismatch: " + name)
            if kind == "matrix" and float(row["duration_s"]) != options["duration"]:
                raise ValueError("matrix duration mismatch: " + name)
        label = name.rsplit("-", 1)[1]
        kind = name.split("-", 1)[0]
        for row in rows:
            key = (kind, row["transport"], int(row["payload_size"]), row.get("strategy", "latency"))
            groups.setdefault(key, {}).setdefault(label, []).append(row)
    result = []
    lines = ["# Controlled comparison", "",
             "Per-condition medians and ranges; per-run percentiles are not pooled percentiles.",
             "No statistical significance or release pass/fail is inferred. Outliers are retained.", "",
             "| Kind | Transport | Bytes | Strategy | Metric | Baseline median (range) | Candidate median (range) | Change |",
             "|---|---|---:|---|---|---:|---:|---:|"]
    for key, variants in sorted(groups.items()):
        for label in ["baseline", "candidate"]:
            if len(variants.get(label, [])) != 2 * options["abba_rounds"]:
                raise ValueError(f"unbalanced comparison: {key} {label}")
        metrics = ["received_mib_sec", "accepted_mib_sec"] if key[0] == "matrix" else ["p50_us", "p99_us"]
        entry = {"condition": key, "metrics": {}, "raw": variants}
        for metric in metrics:
            summaries = {}
            for label, rows in variants.items():
                values = [float(row[metric]) for row in rows]
                if not all(math.isfinite(v) and v >= 0 for v in values):
                    raise ValueError(f"invalid {metric}")
                summaries[label] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
            a, b = summaries["baseline"], summaries["candidate"]
            delta = (b["median"] / a["median"] - 1) * 100 if a["median"] else None
            entry["metrics"][metric] = {"versions": summaries, "change_pct": delta}
            cells = [f'{x["median"]:.3f} ({x["min"]:.3f}–{x["max"]:.3f})' for x in [a, b]]
            change = f"{delta:+.1f}%" if delta is not None else "n/a (zero baseline)"
            lines.append("| " + " | ".join(map(str, key)) + " | " + metric + " | " + " | ".join(cells) + " | " + change + " |")
        result.append(entry)
    lines += ["", "Raw admission, received, failed-send and legacy-drop fields are retained in comparison.json.",
              "Legacy drops do not identify cause-specific accepted-work losses. UDP delivery is not guaranteed by Reliable admission.",
              "Inspect metadata.json and telemetry for clock, temperature, CPU placement and build differences.",
              "The runner does not assert equal toolchains or fixed clocks; check controller logs when used."]
    return result, "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    root = parser.parse_args().directory
    result, markdown = summarize(root)
    (root / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    (root / "comparison.md").write_text(markdown)
    print(root / "comparison.md")


if __name__ == "__main__":
    main()
