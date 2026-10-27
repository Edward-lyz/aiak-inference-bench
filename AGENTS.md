# Project rules

- Follow the parent workspace instructions.
- Frontend: Notion-style monochrome. Use layout, standard-size labels, filters and table columns to communicate. No explanatory captions, footnotes, tiny helper text or colorful accents.
- Call native Docker and SGLang CLIs. Keep this a small benchmark pipeline; do not introduce a generic orchestration framework.
- Never publish fixtures or label smoke/short/failed measurements as qualified capacity.
- Internal configuration, raw evidence and result data stay out of GitHub. Keep local configs ignored.
- On 2026-10-27 the user explicitly authorized starting the overnight 8-GPU tests and terminating other GPU compute processes encountered during this run. Scope termination to GPU compute PIDs, preserve this benchmark's own processes, and record interference. Default runs still refuse foreign processes unless the explicit termination option is supplied.
