# Cookbook baseline reset — 2026-10-28

User decision: retire all previous measurements, use the official high-throughput recipe for P and low-latency recipe for D, enable the model's official speculative decoding, make the code repository public, and continue tuning.

## Source and interpretation

Source: https://lmsysorg.mintlify.app/cookbook/autoregressive/DeepSeek/DeepSeek-V4_1

The existing checkpoint is Flash, not Flash-1017. The linked page has two independent selectors: the URL fragment selects a deployment recipe, while `ax_*` selects an agentic long-context example. Use the verified B200 / Flash / FP4 / single-node deployment cells as the baseline:

- P high throughput: TP4, EP4, max-running-requests256, automatic reasoning/tool parsers, no speculation.
- D low latency: TP4, EP4, memory fraction0.8, DSPARK, draft block size5, decode graph max batch64, automatic parsers. The model uses DSpark rather than an EAGLE/MTP implementation. The official high-throughput cell intentionally disables speculation.
- Do not override attention/MoE/GEMM backends. Record their resolved runtime values.
- Four GPUs per instance are the cookbook baseline. Eight-GPU scaling is a separate tuning configuration, not an invisible change to the official recipe.

## Files and behavior changes

1. `.github/workflows/benchmark.yml`: use masked secrets for internal configuration paths. Audit all reachable Git blobs and self-hosted Actions logs before changing repository visibility. Archive private logs locally and remove their remote copies before publication. Keep dispatch-only trusted-main self-hosted execution.
2. `scripts/run_benchmark.py`: replace fixed TP8/no-speculation/fake-KV assumptions with explicit cookbook strategy, real Prefill for D, phase-specific DSpark validation, dataset provenance, resolved-argument checks, and native acceptance-length recording. Use native SGLang clients and their existing ShareGPT-based fixed-length random dataset; mount a pinned local dataset file.
3. `scripts/summarize.py`, `schemas/summary.schema.json`: include protocol/strategy/dataset/DSpark/cache metadata in comparison identity. P primary metric is input tok/s; D primary metric is native mean TPOT (lower is better), with E2E output throughput retained separately. Never describe a real-prefill E2E throughput as isolated Decode throughput.
4. `web/index.html`, `web/app.js`, `README.md`: display the actual strategy, speculation, KV, cache and primary metric. Remove hardcoded “MTP off” and synthetic-KV labels. D charts/ranking use TPOT, with per-request speed derived from TPOT explicitly labeled.
5. `tests/test_summary.py`, `configs/benchmark.example.json`: verify new metric direction, protocol separation, DSpark metadata and qualification. Keep raw native results internal.
6. Ignored local configs: fresh protocol `sglang-cookbook-v2`; P output1, D output512; fixed input lengths remain explicit. Disable prefix reuse as a recorded cold-cache benchmark condition. Use the official recipe's GPU count and defaults; record any checkpoint/image or native-client adaptation.

## Prior data and execution

- Stop old running jobs, archive the entire old result directory outside the active scan root, write a retirement reason, and publish an empty current summary. The raw evidence remains available for debugging but cannot enter current rankings.
- Audit the available community image against official DSpark flags. The cookbook recommends the `dev-dsv41` preview image; pin its content identity if reachable. A network failure must not be represented as successful use of that image.
- Start with community smoke for both new strategies, then validate AIAK compatibility. An incompatible AIAK release is a recorded blocker, never silently run with speculation disabled.
- Check successful requests, exact token counts, actual speculative acceptance, runtime topology/backends and measurement durations before capacity scans.

## Risks

- DSpark acceptance depends on the prompt corpus. ShareGPT prompts repeated/truncated to a fixed length are a synthetic benchmark workload, not production agent traces; record this recipe and do not infer universal latency.
- Real Prefill changes E2E duration. D must be evaluated from the native TPOT metric, not an assumed acceptance multiplier or fake KV.
- Existing images may lack cookbook flags or contain release bugs. Keep hotfix identity explicit.
- Shared GPU interference can invalidate a repeat. Retain failures and do not silently qualify interrupted groups.
- Public repositories do not remove all Actions limits. Self-hosted compute is already free; standard GitHub-hosted public runs are free, subject to concurrency and other service limits.
