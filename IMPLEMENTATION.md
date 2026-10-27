# Implementation plan and status

Updated: 2026-10-27 (Asia/Shanghai).

User approved implementation in `PUBLIC_REPO/aiak-inference-bench`, with DeepSeek-V4.1-Flash, an internal mirror of community nightly and an aiak_sglang release. GitHub repository is private under Edward-lyz; results stay internal.

| Files | Previous behavior | Intended behavior | Risks / validation |
|---|---|---|---|
| `configs/benchmark.example.json`, local ignored configs | Manual engine launches | Same shared topology/workload; explicit engine image differences | Pin source/image IDs, model revision; check actual launch support |
| `scripts/run_benchmark.py` | Manual test commands and output collection | Native Docker and SGLang CLI orchestration, host lock, named-container cleanup, raw evidence | Real P/D smoke first; refuse foreign GPU processes |
| `scripts/summarize.py`, `schemas/summary.schema.json` | Manual CSV interpretation | Minimal field mapping and qualification, no new load generator | Test failures, short runs, nonfinite/missing intervals, comparison keys |
| `web/*`, `package*.json` | No frontend | Static native browser controls + ECharts, no database/API | Empty state, real-result import, mobile layout, no fabricated results |
| `.github/workflows/*` | No runner pipeline | Manual trusted GPU runs; CPU checks and artifact connectivity probe | Actual runner heartbeat and artifact roundtrip; internal results only |
| `README.md`, `THIRD_PARTY_NOTICES.md` | Untracked operational knowledge | Reproducible commands, caveats and component origins | Keep private configs/logs outside Git |

Implementation decisions:

- JSON configs use the Python standard library; no YAML/config abstraction.
- One fixed community client image runs against both engine images.
- Native `steady_state_serving` replaces a custom window extractor. Qualification uses ≥3 native full measurements after warmup, each ≥120 seconds, and repeat CV ≤5%. Native longest intervals remain diagnostic.
- D uses native fake KV, real expert routing, MTP off. No estimated acceptance or GPU multiplier.
- Sweep boundaries are displayed; only measured, qualified points may rank.
- UI follows the user's Notion-style requirement: monochrome, normal-size field labels and columns, no explanatory captions or footnotes.
- B200dev Docker 20.10.5 prevents thread creation in the newer community image. The unmodified Moby v20.10.24 profile also fails because the installed runtime cannot install its `clone3` rule. Native `seccomp=unconfined` for this project's containers passes the CPU thread probe; this container-level exception is explicit in the local config and run manifest.

Status:

- Private repository and dedicated non-root B200dev runner created.
- Community image and aiak release are locally available and source SHAs resolved.
- Code and Notion-style static frontend implemented. Local qualification/failure handling tests pass.
- GitHub-hosted checks passed; self-hosted runner artifact roundtrip passed (40500287026).
- Self-hosted configuration/build/internal-publication validation passed (40503890924). This was `validate_only=true`, not a GPU benchmark.
- Internal static service is deployed. Browser checks covered JSON import, CSV export and P/D switching; fixtures were not published.
- The user authorized the overnight eight-GPU run. Community P/D and AIAK D smoke passed; AIAK release P has a reproducible completion-path defect. Earlier native CLI failures are retained as evidence.
- Fixed-length offline input generation has been verified with the pinned native client for 1Ki/64Ki inputs and 1/512 outputs.
- Capacity scanning proceeds only for the three engine/phase pairs with successful smoke points. AIAK release P remains failed; a separately identified hotfix needs a baseline decision.

## Authorized overnight run — 2026-10-27

User authorized the eight-GPU run and termination of other GPU compute processes.

- Update the existing ownership check in `scripts/run_benchmark.py` with an explicit root-only termination option. Default behavior remains refusal. Use native signals against the exact observed PIDs, with process-identity checks, and record any interference.
- Validate and run community/aiak P/D smoke; resolve launch/client incompatibilities using native flags.
- Run the fixed 64Ki workload, concurrency scan and repeated measurements. Increase request counts if native full measurements are too short; retain failed/contaminated measurements.
- Publish real summaries to the existing internal static site and preserve raw evidence.
- Risks: shared jobs may restart; terminating an in-flight foreign process does not make an overlapping measurement valid. Such points must fail rather than enter capacity rankings.
- Observed cold-start DeepGEMM compilation takes 10–20 minutes. Reuse per-engine native cache directories through Docker bind mounts during this overnight run; remove the task-owned caches after completion.
- Capacity points start with a bounded native request count and increase it when the native full timed measurement is below 120 seconds, targeting 150 seconds. Preserve every native attempt and reuse the chosen count for subsequent repetitions; do not reduce the qualification threshold.
- Update `web/app.js` filter ordering so the selected profile determines available contexts, and new page loads prefer the most recent comparison group. This keeps real 64Ki capacity runs visible alongside earlier 1Ki smoke and diagnostic runs.
- AIAK release P fails when a request finishes during prefill: the frozen result-processor dataclass has no `server_args`, but its one-token completion branch reads `self.server_args.enable_metrics`. Preserve the release failure. Continue both validated D scans and community P; prepare a reviewable one-line native-component fix separately before deciding whether a hotfix comparison is acceptable.
- Real closed-loop D evidence invalidated the original continuous-interval gate: concurrency1 smoke took 1.373s overall but its longest native interval was 0.179s; the first 64Ki capacity attempt completed16 requests in41.379s and again selected only a single-request interval. More requests cannot bridge the idle gaps between sequential requests. Change `scripts/run_benchmark.py`, `scripts/summarize.py`, configs, tests and UI terminology to rank the native full timed measurement after warmup, require >=120s per repeat and CV<=5%, and retain native longest intervals only as diagnostics. Derive full-window mean active concurrency from completed × mean native E2E latency / native duration. Include measurement basis in comparison keys so old and new metrics never merge.
