# AIAK Inference Bench

Compare community SGLang nightly and an aiak_sglang release on the same model and GPU host. No TTFT/TPOT/OTPS thresholds. This is an independent private benchmark workspace, not an official InferenceX deployment.

## Run

Use Python 3.10+, Docker with NVIDIA support, and a GPU host with a coordinated exclusive window. The runner refuses to start when any other compute process is present. It checks for interference every five seconds during measurement and never stops another user's process.

```sh
uv venv .venv
cp configs/benchmark.example.json configs/b200dev.local.json
# Fill in the local model path, engine images and result directory.
.venv/bin/python scripts/run_benchmark.py --config configs/b200dev.local.json --dry-run
.venv/bin/python scripts/run_benchmark.py --config configs/b200dev.local.json \
  --profile smoke --engine all --phase all
```

Use a separate local smoke config with short input/output lengths, a small concurrency range and one repetition. `smoke` is a quality label; it does not silently override the workload config. For formal scans use the full config and `--profile capacity`.

Images must already be present (`docker pull` explicitly before the run). Each image is resolved to a local content ID, repository digest and source SHA once per run. Both engines use the same community image's native benchmark client. Weights are mounted read-only; keep their revision fixed during the entire run.

Private local configs, results, raw logs and site data are ignored by Git. Repository directory name `PUBLIC_REPO` does not change the GitHub repository's private visibility.

## What the numbers mean

| Phase | Server | Native client | Reported quantity |
|---|---|---|---|
| P | Unified server, cache off, one output token | `sglang.benchmark.steady_state_serving` | Input tokens/s |
| D | Decode server, `--disaggregation-transfer-backend fake`, real expert routing | Same client, `--fake-prefill` | Output tokens/s, explicitly synthetic KV |

MTP is off. No manual acceptance multiplier or GPU-count multiplier is applied. D synthetic KV measures capacity, not accuracy or production 1P1D throughput. Ordinary long-output E2E tests are not substituted for isolated D.

Native steady-state reporting selects the longest continuous interval at ≥80% of peak client concurrency. A capacity point requires at least three independent repetitions; every interval must last ≥120 seconds with average concurrency ≥80% of target; repeat throughput CV must be ≤5%, and all requests must succeed. The reported value is the median across repetitions. This measures cross-run repeatability, not within-run window CV.

Short intervals stay unqualified. Increase `num_prompts` if needed; do not lower the threshold to turn a short run into a stable result. The sweep covers only `concurrencies` in the config. The dashboard never claims a global maximum, and compares peaks only within one comparison group and run. Hardware, model revision/config hash, precision, workload, client image and shared launch arguments are part of the comparison key.

Results retain full-run throughput, native steady-state throughput, TTFT, TPOT, all repetitions and failures. Failed points are not represented as zero. Server failures are also retained. Each point has the exact argv and original native client output in its internal evidence directory.

## Dashboard

```sh
npm ci --ignore-scripts
python3 scripts/summarize.py /path/to/results web/data/summary.json
npm run dev
# Or build an internal static site:
npm run build
mkdir -p dist/data
cp web/data/summary.json dist/data/summary.json
python3 -m http.server 31880 --directory dist --bind 127.0.0.1
```

The frontend bundles pinned ECharts locally and does not depend on a CDN, database or business API. It supports P/D, model/context/profile/comparison-group filters, native curve rendering, latency details, JSON import/export and CSV export. Its initial state contains no synthetic results. `schemas/summary.schema.json` documents the exported contract.

## GitHub Actions

Only trusted `main` may run GPU jobs. All GPU workflows are manual and serialized. No pull-request code runs on the internal runner.

Repository variables (private paths are configured out-of-band):

| Variable | Value |
|---|---|
| `BENCH_CONFIG` | Absolute path to the capacity local config |
| `BENCH_SMOKE_CONFIG` | Absolute path to the smoke local config |
| `BENCH_RESULTS_DIR` | Internal evidence directory matching the configs |
| `BENCH_SITE_DIR` | Internal static web root owned by this project |

`Runner connectivity` verifies a non-sensitive artifact upload/download roundtrip. `P-D capacity benchmark` builds, executes serially, and publishes the summary internally, including failures. It never uploads benchmark logs or results as GitHub artifacts. `Verify frontend and result handling` runs CPU-only checks on GitHub-hosted infrastructure.

The GPU workflow uses a dedicated non-root runner and Docker socket access. This runner is trusted infrastructure: keep repository write access restricted. Cancellation only removes containers named by the active run. A host `flock` also prevents overlapping manual and CI jobs.

## Checks

```sh
.venv/bin/python -m unittest discover -s tests -v
npm run build
```

Tests cover qualification thresholds, repeat CV, missing intervals, failure retention and preventing comparisons across model revisions or runs. Real GPU validation is tracked separately in `IMPLEMENTATION.md`.
