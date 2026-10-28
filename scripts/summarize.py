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
    identities_by_run = {}
    for manifest_path in source.glob("*/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        identities = {identity["id"]: identity for identity in manifest["engines"].values()}
        sidecar = manifest_path.with_name("image-identities.json")
        if sidecar.exists():
            for image_id, verified in json.loads(sidecar.read_text(encoding="utf-8")).items():
                if image_id not in identities or verified["source_sha"] != identities[image_id]["source_sha"]:
                    raise ValueError(f"Version sidecar does not match manifest: {manifest_path.parent.name}")
                identities[image_id] = {**identities[image_id], **verified}
        identities_by_run[manifest_path.parent.name] = identities
    for point_file in sorted(source.glob("*/*/point.json")):
        point = json.loads(point_file.read_text(encoding="utf-8"))
        key_fields = {
            key: point[key] for key in (
                "model", "model_revision", "model_config_sha256", "precision",
                "phase", "measurement_mode", "fake_kv", "fake_ep", "mtp", "cache_mode",
                "profile", "input_length", "output_length", "gpu_count", "hardware",
                "seed", "client_image_id",
            )
        }
        key_fields.update({key: point.get(key) for key in (
            "dataset", "tokenized_prompt", "random_range_ratio", "measurement_basis",
            "protocol_id", "strategy", "speculative_algorithm", "speculative_block_size", "dataset_sha256",
        )})
        comparison_key = hashlib.sha256(json.dumps(key_fields, sort_keys=True).encode()).hexdigest()[:20]
        public = {key: point[key] for key in (
            "run_id", "timestamp", "engine", "engine_sha", "model", "model_revision",
            "precision", "phase", "measurement_mode", "profile", "input_length",
            "output_length", "concurrency", "repetition", "gpu_count", "status",
        )}
        public["dataset"] = point.get("dataset")
        public["num_prompts"] = point.get("num_prompts")
        public["engine_variant"] = point.get("engine_variant", "unmodified")
        identity = identities_by_run.get(point["run_id"], {}).get(point.get("image_id"), {})
        if identity and identity["source_sha"] != point["engine_sha"]:
            raise ValueError(f"Point source does not match image identity: {point_file.parent.name}")
        provenance = identity.get("version_provenance", {})
        public.update(
            engine_version=provenance.get("version"),
            engine_package_version=identity.get("package_version"),
            engine_version_relation=provenance.get("relation"),
            engine_base_sha=provenance.get("base_source_sha"),
            engine_release_ahead=provenance.get("ahead_by"),
            engine_release_behind=provenance.get("behind_by"),
        )
        public["measurement_basis"] = point.get("measurement_basis", "native-steady-interval")
        public.update({key: point.get(key) for key in (
            "protocol_id", "strategy", "speculative_algorithm", "speculative_block_size", "dataset_sha256",
            "fake_kv", "fake_ep", "cache_mode",
        )})
        latency_metric = point.get("protocol_id") == "sglang-cookbook-v2" and point["phase"] == "D"
        public.update(metric_name="tpot_ms" if latency_metric else "input_throughput" if point["phase"] == "P" else "output_throughput",
                      metric_unit="ms" if latency_metric else "tok/s", lower_is_better=latency_metric,
                      metric_value=None, accept_length=None, p99_tpot_ms=None)
        # Full diagnostic strings may contain local paths; keep them in local raw evidence.
        public.update(comparison_key=comparison_key, error="See local run evidence" if point["error"] else None,
                      throughput=None, full_throughput=None, duration=None, ttft_ms=None, tpot_ms=None,
                      stable=False)
        if point["status"] == "ok":
            metric = "input_throughput" if point["phase"] == "P" else "output_throughput"
            steady = point["steady"]
            measurement = point.get("measurement", steady)
            public.update(
                throughput=measurement[metric], full_throughput=point["metrics"][metric],
                duration=measurement["duration"], average_concurrency=measurement["average_concurrency"],
                native_steady_throughput=steady[metric], native_steady_duration=steady["duration"],
                ttft_ms=point["metrics"]["mean_ttft_ms"],
                tpot_ms=point["metrics"]["mean_tpot_ms"] if point["output_length"] > 1 else None,
                accept_length=point["metrics"].get("accept_length"),
                p99_tpot_ms=point["metrics"].get("p99_tpot_ms"),
            )
            public["metric_value"] = public["tpot_ms"] if latency_metric else public["throughput"]
            if public["throughput"] is None or not math.isfinite(public["throughput"]) or public["throughput"] <= 0:
                public.update(status="invalid", error="No usable native measurement interval", throughput=None)
            if public["metric_value"] is None or not math.isfinite(public["metric_value"]) or public["metric_value"] <= 0:
                public.update(status="invalid", error="No usable native primary metric", metric_value=None)
            if latency_metric and (not public["accept_length"] or not math.isfinite(public["accept_length"]) or public["accept_length"] <= 0):
                public.update(status="invalid", error="No native DSpark acceptance measurement")
        if public["status"] == "ok":
            groups[(comparison_key, point["engine"], point["run_id"], point["concurrency"], point.get("num_prompts"))].append((public, point))
        else:
            failed_groups.add((comparison_key, point["engine"], point["run_id"], point["concurrency"]))
        points.append(public)
    series = []
    for (comparison_key, engine, run_id, concurrency, num_prompts), samples in groups.items():
        rates = [sample[0]["throughput"] for sample in samples]
        values = [sample[0]["metric_value"] for sample in samples]
        cv = statistics.pstdev(values) / statistics.mean(values) if len(values) > 1 else None
        representative, raw_point = samples[0]
        stable = (
            representative["profile"] == "capacity"
            and (comparison_key, engine, run_id, concurrency) not in failed_groups
            and len({sample[0]["repetition"] for sample in samples}) >= 3
            and all(sample[0]["duration"] >= sample[1].get(
                "min_measurement_seconds", sample[1].get("min_steady_seconds", 120)
            ) for sample in samples)
            and all(sample[0]["average_concurrency"] >= 0.8 * concurrency for sample in samples)
            and cv is not None and cv <= raw_point["max_cv"]
        )
        for sample, original in samples:
            sample["stable"] = stable
        series.append({
            **{key: representative[key] for key in (
                "model", "model_revision", "phase", "profile", "input_length", "output_length",
                "measurement_mode", "measurement_basis", "precision", "gpu_count", "engine_sha", "engine_variant",
                "engine_version", "engine_package_version", "engine_version_relation", "engine_base_sha",
                "engine_release_ahead", "engine_release_behind",
                "protocol_id", "strategy", "speculative_algorithm", "speculative_block_size", "dataset_sha256",
                "metric_name", "metric_unit", "lower_is_better", "fake_kv", "fake_ep", "cache_mode",
            )},
            "comparison_key": comparison_key, "engine": engine, "run_id": run_id, "concurrency": concurrency,
            "num_prompts": num_prompts,
            "throughput": statistics.median(rates), "min": min(rates), "max": max(rates),
            "metric_value": statistics.median(values),
            "accept_length": None if representative["accept_length"] is None else statistics.median(s[0]["accept_length"] for s in samples),
            "cv": cv, "repeats": len(rates), "stable": stable,
            "ttft_ms": statistics.median(s[0]["ttft_ms"] for s in samples),
            "tpot_ms": None if representative["tpot_ms"] is None else statistics.median(s[0]["tpot_ms"] for s in samples),
        })
    failures = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(source.glob("*/*-failure.json"))]
    summary = {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "points": points, "series": series,
        "failures": [{key: failure[key] for key in ("run_id", "engine", "phase", "status", "stage")} for failure in failures],
        "method": "Official cookbook strategies: P high-throughput input tok/s; D low-latency DSpark with real Prefill, native TPOT in ms (lower is better). D E2E output throughput is auxiliary. Qualification: >=3 repeats, each >=120s, primary-metric CV<=5%, active concurrency>=80% target. Dataset uses ShareGPT prompts repeated/truncated to fixed lengths. Native acceptance length is reported, never assumed. Retired pre-cookbook evidence is outside the active result root.",
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
