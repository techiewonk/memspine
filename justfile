# memspine dev commands — run `just <recipe>`
set shell := ["bash", "-cu"]

# install dev environment with all extras
setup:
    uv sync --all-extras

# run the test suite
test:
    uv run pytest

# lint + format-check + typecheck
lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy

# auto-fix formatting and lint
fix:
    uv run ruff format .
    uv run ruff check --fix .

# pre-commit gate: lint then test
check: lint test

# serve docs locally
docs:
    uv run mkdocs serve -f docs/mkdocs.yml

# run the combination-matrix boot tests
combos:
    uv run pytest tests/combinations -q

# show the effective config for a template (e.g. just resolve personal)
resolve template="base":
    uv run memspine config resolve --template {{template}}

# install the evals environment (G1 / ENV-1): CUDA torch, bitsandbytes, sentence-transformers,
# datasets, and huggingface-hub<2. Run once per machine; then `bash evals/run.sh ...`.
evals-setup:
    uv sync --extra dev
    uv pip install --index-url https://download.pytorch.org/whl/cu128 --extra-index-url https://pypi.org/simple torch
    uv pip install bitsandbytes sentence-transformers datasets pyarrow jsonschema "huggingface-hub<2"
    uv run python -c "import torch, sentence_transformers, datasets, huggingface_hub, bitsandbytes as b; print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.is_available()); print('sentence-transformers', sentence_transformers.__version__); print('datasets', datasets.__version__); print('huggingface-hub', huggingface_hub.__version__); print('bitsandbytes', b.__version__)"
