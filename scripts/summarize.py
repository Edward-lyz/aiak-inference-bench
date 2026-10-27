"""Export a safe dashboard summary without prompts, private paths, or raw server info."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics


def build_summary(source: Path, destination: Path) -> dict:
    points = []
    groups = defaultdict(list)
    failed_groups = set()
    for point_file in sorted(source.glob("*/*/point.json")):
        point = json.loads(point_file.read_text(encoding="utf-8"))
        key_fields = {
            key: point[key] for key in (
                "model", "model_revision", "model_config_sha256", "precision",
                "phase", "measurement_mode", "fake_kv", "fake_ep", "mtp", "cache_mode",
                "profile", "input_length", "output_length", "gpu_count", "hardware",
                "seed", "server_args", "client_image_id",
            )
        }
        comparison_key = hashlib.sha256(json.dumps(key_fields, sort_keys=True).encode()).hexdigest()[:20]
        public = {key: point[key] for key in (
            "run_id", "timestamp", "engine", "engine_sha", "model", "model_revision",
            "precision", "phase", "measurement_mode", "profile", "input_length",
            "output_length", "concurrency", "repetition", "gpu_count", "status",
        )}
        # Full diagnostic strings may contain local paths; keep them in local raw evidence.
        public.update(comparison_key=comparison_key, error="See local run evidence" if point["error"] else None,
                      throughput=None, full_throughput=None, duration=None, ttft_ms=None, tpot_ms=None,
                      stable=False)
        if point["status"] == "ok":
            metric = "input_throughput" if point["phase"] == "P" else "output_throughput"
            steady = point["steady"]
            public.update(
                throughput=steady[metric], full_throughput=point["metrics"][metric],
                duration=steady["duration"], average_concurrency=steady["average_concurrency"],
                ttft_ms=point["metrics"]["mean_ttft_ms"],
                tpot_ms=point["metrics"]["mean_tpot_ms"] if point["output_length"] > 1 else None,
            )
            if public["throughput"] is None or not math.isfinite(public["throughput"]) or public["throughput"] <= 0:
                public.update(status="invalid", error="No usable native steady-state interval", throughput=None)
        if public["status"] == "ok":
            groups[(comparison_key, point["engine"], point["run_id"], point["concurrency"])].append((public, point))
        else:
            failed_groups.add((comparison_key, point["engine"], point["run_id"], point["concurrency"]))
        points.append(public)
    series = []
    for (comparison_key, engine, run_id, concurrency), samples in groups.items():
        rates = [sample[0]["throughput"] for sample in samples]
        cv = statistics.pstdev(rates) / statistics.mean(rates) if len(rates) > 1 else None
        representative, raw_point = samples[0]
        stable = (
            representative["profile"] == "capacity"
            and (comparison_key, engine, run_id, concurrency) not in failed_groups
            and len({sample[0]["repetition"] for sample in samples}) >= 3
            and all(sample[0]["duration"] >= sample[1]["min_steady_seconds"] for sample in samples)
            and all(sample[0]["average_concurrency"] >= 0.8 * concurrency for sample in samples)
            and cv is not None and cv <= raw_point["max_cv"]
        )
        for sample, original in samples:
            sample["stable"] = stable
        series.append({
            **{key: representative[key] for key in (
                "model", "model_revision", "phase", "profile", "input_length", "output_length",
                "measurement_mode", "precision", "gpu_count", "engine_sha",
            )},
            "comparison_key": comparison_key, "engine": engine, "run_id": run_id, "concurrency": concurrency,
            "throughput": statistics.median(rates), "min": min(rates), "max": max(rates),
            "cv": cv, "repeats": len(rates), "stable": stable,
            "ttft_ms": statistics.median(s[0]["ttft_ms"] for s in samples),
            "tpot_ms": None if representative["tpot_ms"] is None else statistics.median(s[0]["tpot_ms"] for s in samples),
        })
    failures = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(source.glob("*/*-failure.json"))]
    summary = {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "points": points, "series": series,
        "failures": [{key: failure[key] for key in ("run_id", "engine", "phase", "status", "stage")} for failure in failures],
        "method": "Native SGLang steady-state interval (>=80% peak concurrency); capacity qualification requires >=3 repeats, each >=120s, repeat CV<=5%, >=80% target concurrency. Smoke is never ranked as capacity.",
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    build_summary(args.source, args.destination)
