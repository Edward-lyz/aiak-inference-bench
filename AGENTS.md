# Project rules

- Follow the parent workspace instructions.
- Frontend: Notion-style monochrome. Use layout, standard-size labels, filters and table columns to communicate. No explanatory captions, footnotes, tiny helper text or colorful accents.
- Call native Docker and SGLang CLIs. Keep this a small benchmark pipeline; do not introduce a generic orchestration framework.
- Never publish fixtures or label smoke/short/failed measurements as qualified capacity.
- Internal configuration, raw evidence and result data stay out of GitHub. Keep local configs ignored.
- Use the official cookbook high-throughput P and low-latency DSpark D strategies. Pre-cookbook measurements are retired and must never re-enter current rankings.
- Resolve the newest CUDA13 amd64 community nightly from the configured registry at each run's start, then pin its digest for the entire run and shared benchmark client.
- Terminating foreign GPU processes requires explicit user authorization for the current work. Scope checks and termination to allocated GPUs, preserve owned processes and record interference. Default runs refuse foreign processes.
