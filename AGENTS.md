# Project rules

- Follow the parent workspace instructions.
- Frontend: Notion-style monochrome. Use layout, standard-size labels, filters and table columns to communicate. No explanatory captions, footnotes, tiny helper text or colorful accents.
- Call native Docker and SGLang CLIs. Keep this a small benchmark pipeline; do not introduce a generic orchestration framework.
- Never publish fixtures or label smoke/short/failed measurements as qualified capacity.
- Internal configuration, raw evidence and result data stay out of GitHub. Keep local configs ignored.
- The user is coordinating the first exclusive 8-GPU window. Do not launch GPU tests until they confirm that window; CPU checks and static publication may proceed.
