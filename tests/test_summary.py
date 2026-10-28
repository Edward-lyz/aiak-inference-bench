"""Qualification and comparability are scientific boundaries, not presentation details."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("summarize", Path(__file__).parents[1] / "scripts/summarize.py")
summarize = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summarize)


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.point = {
            "run_id": "test-run", "timestamp": "2026-10-27T00:00:00+00:00",
            "engine": "community", "engine_sha": "a" * 40, "client_image_id": "sha256:client",
            "model": "test-fixture", "model_revision": "test", "model_config_sha256": "a" * 64,
            "precision": "fp8", "phase": "P", "measurement_mode": "client", "fake_kv": False,
            "fake_ep": False, "mtp": False, "cache_mode": "off", "profile": "capacity",
            "input_length": 1024, "output_length": 1, "gpu_count": 8, "hardware": "test",
            "seed": 42, "server_args": [], "concurrency": 4, "repetition": 1,
            "status": "ok", "error": None, "min_steady_seconds": 120, "max_cv": 0.05,
            "metrics": {"input_throughput": 95, "mean_ttft_ms": 10, "mean_tpot_ms": 0},
            "steady": {"input_throughput": 100, "duration": 130, "average_concurrency": 4},
        }

    def write_repetitions(self, rates: list[float | None], point: dict | None = None) -> dict:
        base = point if point is not None else self.point
        for repetition, rate in enumerate(rates, 1):
            record = copy.deepcopy(base)
            record["repetition"] = repetition
            record["steady"]["input_throughput"] = rate
            directory = self.root / record["run_id"] / f"{record['engine']}-{repetition}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "point.json").write_text(json.dumps(record), encoding="utf-8")
        return summarize.build_summary(self.root, self.root / "summary.json")

    def test_three_long_repetitions_qualify_with_median(self) -> None:
        report = self.write_repetitions([99, 100, 101])
        self.assertTrue(report["series"][0]["stable"])
        self.assertEqual(report["series"][0]["throughput"], 100)

    def test_short_runs_and_smoke_do_not_qualify(self) -> None:
        self.point["steady"]["duration"] = 119
        self.assertFalse(self.write_repetitions([100] * 3)["series"][0]["stable"])
        self.point["steady"]["duration"] = 130
        self.point["profile"] = "smoke"
        self.assertFalse(self.write_repetitions([100] * 3)["series"][0]["stable"])

    def test_variation_and_undersaturation_do_not_qualify(self) -> None:
        self.assertFalse(self.write_repetitions([50, 100, 150])["series"][0]["stable"])
        self.point["steady"]["average_concurrency"] = 2
        self.assertFalse(self.write_repetitions([100] * 3)["series"][0]["stable"])

    def test_different_model_revisions_have_different_comparison_keys(self) -> None:
        first = self.write_repetitions([100])
        self.point["model_revision"] = "different-weights"
        second = self.write_repetitions([100])
        self.assertNotEqual(first["points"][0]["comparison_key"], second["points"][0]["comparison_key"])

    def test_failure_is_preserved_without_private_error_text(self) -> None:
        self.point.update(status="failed", error="secret path /internal/private")
        report = self.write_repetitions([100])
        self.assertEqual(report["points"][0]["status"], "failed")
        self.assertEqual(report["points"][0]["throughput"], None)
        self.assertEqual(report["series"], [])
        self.assertNotIn("/internal/private", json.dumps(report))

    def test_repetitions_from_different_runs_do_not_combine(self) -> None:
        self.write_repetitions([100, 100])
        self.point["run_id"] = "another-run"
        report = self.write_repetitions([100])
        self.assertEqual(len(report["series"]), 2)
        self.assertFalse(any(series["stable"] for series in report["series"]))

    def test_no_steady_interval_does_not_become_zero(self) -> None:
        report = self.write_repetitions([None])
        self.assertEqual(report["points"][0]["status"], "invalid")
        self.assertIsNone(report["points"][0]["throughput"])
        self.assertEqual(report["series"], [])

    def test_one_failed_repeat_disqualifies_other_successful_repeats(self) -> None:
        self.write_repetitions([100, 100, 100])
        failure = copy.deepcopy(self.point)
        failure.update(repetition=4, status="failed", error="request failed")
        directory = self.root / failure["run_id"] / "community-4"
        directory.mkdir()
        (directory / "point.json").write_text(json.dumps(failure), encoding="utf-8")
        report = summarize.build_summary(self.root, self.root / "summary.json")
        self.assertFalse(report["series"][0]["stable"])

    def test_full_measurement_qualifies_despite_fragmented_diagnostic_intervals(self) -> None:
        self.point.update(
            measurement_basis="native-full-run", min_measurement_seconds=120,
            measurement={"input_throughput": 95, "duration": 150, "average_concurrency": 3.9},
        )
        self.point["steady"]["duration"] = 0.2
        report = self.write_repetitions([1000, 2000, 3000])
        self.assertTrue(report["series"][0]["stable"])
        self.assertEqual(report["series"][0]["throughput"], 95)
        self.assertEqual(report["points"][0]["native_steady_duration"], 0.2)
        self.assertEqual(report["points"][0]["duration"], 150)

    def test_short_full_measurements_never_borrow_legacy_interval_qualification(self) -> None:
        self.point.update(
            measurement_basis="native-full-run", min_measurement_seconds=120,
            measurement={"input_throughput": 95, "duration": 119, "average_concurrency": 4},
        )
        self.assertFalse(self.write_repetitions([100] * 3)["series"][0]["stable"])

    def test_measurement_bases_have_distinct_comparison_keys(self) -> None:
        first = self.write_repetitions([100])
        self.point.update(
            measurement_basis="native-full-run", min_measurement_seconds=120,
            measurement={"input_throughput": 100, "duration": 130, "average_concurrency": 4},
        )
        second = self.write_repetitions([100])
        self.assertNotEqual(first["points"][0]["comparison_key"], second["points"][0]["comparison_key"])

    def test_hotfix_identity_survives_public_export(self) -> None:
        self.point["engine_variant"] = "metrics-guard-hotfix"
        report = self.write_repetitions([100] * 3)
        self.assertEqual(report["points"][0]["engine_variant"], "metrics-guard-hotfix")
        self.assertEqual(report["series"][0]["engine_variant"], "metrics-guard-hotfix")

    def test_cookbook_decode_uses_latency_and_requires_acceptance(self) -> None:
        self.point.update(
            protocol_id="sglang-cookbook-v2", phase="D", output_length=512,
            strategy="low-latency", speculative_algorithm="DSPARK", speculative_block_size=5,
            measurement_mode="real-prefill-decode-latency", dataset_sha256="b" * 64,
        )
        self.point["steady"]["output_throughput"] = 100
        self.point["metrics"].update(output_throughput=95, mean_tpot_ms=2, accept_length=3.5)
        report = self.write_repetitions([100] * 3)
        series = report["series"][0]
        self.assertTrue(series["stable"])
        self.assertTrue(series["lower_is_better"])
        self.assertEqual(series["metric_name"], "tpot_ms")
        self.assertEqual(series["metric_value"], 2)
        self.assertEqual(series["throughput"], 100)
        self.assertEqual(series["accept_length"], 3.5)
        self.point["metrics"]["accept_length"] = None
        self.assertEqual(self.write_repetitions([100] * 3)["series"], [])

    def test_decode_qualification_uses_tpot_variation_not_e2e_throughput(self) -> None:
        self.point.update(protocol_id="sglang-cookbook-v2", phase="D", output_length=512)
        self.point["steady"]["output_throughput"] = 100
        self.point["metrics"].update(output_throughput=100, mean_tpot_ms=2, accept_length=3)
        self.write_repetitions([100] * 3)
        path = self.root / "test-run" / "community-3" / "point.json"
        changed = json.loads(path.read_text())
        changed["metrics"]["mean_tpot_ms"] = 10
        path.write_text(json.dumps(changed))
        report = summarize.build_summary(self.root, self.root / "summary.json")
        self.assertFalse(report["series"][0]["stable"])

    def test_cookbook_protocol_and_dataset_cannot_merge_with_old_measurements(self) -> None:
        original = self.write_repetitions([100])
        self.point.update(protocol_id="sglang-cookbook-v2", dataset_sha256="b" * 64)
        cookbook = self.write_repetitions([100])
        self.assertNotEqual(original["points"][0]["comparison_key"], cookbook["points"][0]["comparison_key"])
        self.point["dataset_sha256"] = "c" * 64
        changed = self.write_repetitions([100])
        self.assertNotEqual(cookbook["points"][0]["comparison_key"], changed["points"][0]["comparison_key"])

    def test_different_prompt_subsets_cannot_combine_for_qualification(self) -> None:
        self.point["num_prompts"] = 32
        self.write_repetitions([100] * 3)
        path = self.root / "test-run" / "community-3" / "point.json"
        changed = json.loads(path.read_text())
        changed["num_prompts"] = 64
        path.write_text(json.dumps(changed))
        report = summarize.build_summary(self.root, self.root / "summary.json")
        self.assertEqual({series["num_prompts"] for series in report["series"]}, {32, 64})
        self.assertFalse(any(series["stable"] for series in report["series"]))


if __name__ == "__main__":
    unittest.main()
