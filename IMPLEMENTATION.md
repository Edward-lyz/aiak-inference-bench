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
- Native `steady_state_serving` replaces a custom window extractor. Qualification uses ≥3 native steady intervals of ≥120 seconds and repeat CV ≤5%. No within-window CV claim.
- D uses native fake KV, real expert routing, MTP off. No estimated acceptance or GPU multiplier.
- Sweep boundaries are displayed; only measured, qualified points may rank.

Status:

- Private repository and dedicated non-root B200dev runner created.
- Community image and aiak release are locally available and source SHAs resolved.
- Code and static frontend implemented; local and CI validation in progress.
- GPU smoke has not run: another user's GPU process was detected. User is coordinating an exclusive window.
- Full capacity scan, paired P/D results and final real-result publication remain pending that window and smoke validation.
