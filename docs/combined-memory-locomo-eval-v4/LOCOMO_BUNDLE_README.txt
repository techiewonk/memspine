Combined memory evaluator transfer bundle

Source repository revision: 070ccd91d859afa478d5ed50ead1fb3c07a69870
Extract into a complete checkout of the same repository revision on the target system.
This archive is an overlay, not a standalone memory engine or installed environment.
Uncommitted evaluator changes are included; the manifest hashes identify their exact contents.
Install the repository dependencies and configure the target system's own services and credentials.
Read docs/COMBINED_MEMORY_ENGINE_AND_LOCOMO.html before running live evaluation.
Offline validation: python -m pytest tests/locomo_eval -o addopts= -q
CLI help: python -m module.locomo_eval --help
No credentials, environment files, caches or benchmark run outputs are included.
