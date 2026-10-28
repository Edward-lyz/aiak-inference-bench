# AIAK Inference Bench

Compare community SGLang and aiak_sglang using the official SGLang cookbook strategies on the same checkpoint and GPU allocation. No TTFT/TPOT/OTPS admission thresholds. This is an independent benchmark, not an official SGLang or InferenceX result.

The code repository is public. Configuration, datasets, raw evidence, results and the dashboard stay internal. All measurements predating the `sglang-cookbook-v2` reset on 2026-10-28 are retired and excluded from current comparisons. See [COOKBOOK_PLAN.md](COOKBOOK_PLAN.md).

## Run

Use Python 3.10+, Docker with NVIDIA support, and a GPU host with a coordinated exclusive window. Default runs refuse foreign GPU compute processes and check for interference every five seconds. An explicitly authorized root run may supply `--terminate-foreign-gpu-processes`; native Linux pidfds target the observed GPU processes without PID-reuse ambiguity. An overlapping measurement is invalidated even when the interfering process is removed.

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

For an automatically updated community baseline, set `community_image_repository` to the designated registry/repository. Before each run the native Registry API lists CUDA13 amd64 nightly tags, selects the newest date (image creation time breaks same-date ties), checks the platform, and pulls its immutable digest. The same image is used for the shared native client. The registry must permit anonymous pull-token access; lookup failures stop the run without falling back to an older image. Other engine images must already be present. No version changes during a run.

The optional local `docker_security_opt` field passes a native Docker security option to project containers. B200dev's Docker 20.10.5 requires `seccomp=unconfined` for the newer image to create threads. This disables syscall filtering inside those containers and is recorded in the manifest; use only the trusted benchmark images configured by the operator. A future host runtime upgrade should remove this exception.

Private local configs, results, raw logs and site data are ignored by Git. GitHub Actions uses masked secrets for internal paths. Self-hosted GPU jobs remain manual, trusted-main dispatches.

## What the numbers mean

| Phase | Server | Native client | Reported quantity |
|---|---|---|---|
| P | Official B200 high-throughput: TP4/EP4, max running256, speculation off | `sglang.benchmark.steady_state_serving`, one output token | Input tokens/s, higher is better |
| D | Official B200 low-latency: TP4/EP4, memory0.8, DSpark block5, decode graph64, max running48 | Same client, real Prefill and generated output | Native mean TPOT in ms, lower is better |

Source: [official DeepSeek-V4.1 cookbook](https://lmsysorg.mintlify.app/cookbook/autoregressive/DeepSeek/DeepSeek-V4_1), verified B200 / Flash / FP4 / single-node cells. Flash-1017 is a different checkpoint. The model's speculation is DSpark; the high-throughput recipe intentionally disables it. Native backend selection is preserved and resolved topology/speculation is checked at runtime.

The D output-throughput column includes real Prefill and is an E2E auxiliary metric, never isolated Decode throughput. Acceptance length comes from the native client's server-reported value; no assumed acceptance or GPU-count multiplier is used. The default cold-cache benchmark disables prefix reuse as an explicit adaptation to the cookbook serving command. Actual image/source identities, GPU allocation, dataset hash and all launch arguments are recorded.

Capacity uses the native client's full timed measurement after warmup. A point requires at least three independent repetitions; every measurement must last ≥120 seconds (`min_measurement_seconds`), average active concurrency must be ≥80% of target, primary-metric CV across repetitions must be ≤5%, and all requests must succeed. The primary metric is P input throughput or D TPOT. Average active concurrency is computed from native completed requests × mean E2E latency / native duration. The reported value is the median across repetitions.

The native longest continuous interval remains diagnostic. Fixed-length closed-loop requests can split that interval into individual requests or batches, so increasing request count does not necessarily lengthen it. Historical interval-based measurements have different comparison keys and never mix with full-window measurements.

The native client uses `random`, a pinned local ShareGPT dataset, `--tokenize-prompt` and `--random-range-ratio 1`. Native sampling repeats or truncates real prompt token sequences to fixed input lengths. This is a controlled synthetic workload; it does not establish production agent-trace acceptance. Dataset contents never enter Git. EOS is ignored by the pinned client's default behavior.

The first engine calibrates request count once per phase/concurrency, targeting 150 seconds for a 120-second minimum, with at most three native attempts. All later repetitions and the other engine reuse that count and seed, so paired measurements use identical prompt subsets. A fixed-count window below 120 seconds remains unqualified; rerun both engines with a larger common count when needed. Different request counts never combine for qualification. The sweep uses `phase_concurrencies` when supplied, otherwise `concurrencies`. The dashboard never claims a global maximum, and compares peaks only within one comparison group and run. Hardware, model revision/config hash, precision, dataset recipe, workload, client image and shared launch arguments are part of the comparison key.

Initial capacity request count is at least four times concurrency. This limits expensive long-input Prefill runs while preserving the measured ≥80% average-concurrency gate; adaptive timing can increase the count further.

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

Repository Actions secrets (private paths are configured out-of-band):

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
