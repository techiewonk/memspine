"""ENV-2: nothing under evals/ may shadow the HuggingFace ``datasets`` package.

The legacy adapter folder used to be ``evals/datasets``; with ``evals/`` on ``sys.path``
(as pytest does) it shadowed the real package once sentence-transformers installed it.
It is now ``evals/legacy_datasets``.
"""

from __future__ import annotations

from pathlib import Path

EVALS = Path(__file__).resolve().parent.parent


def test_no_datasets_package_inside_evals() -> None:
    assert not (EVALS / "datasets").exists(), "evals/datasets would shadow HuggingFace `datasets`"
    assert not (EVALS / "datasets.py").exists()


def test_legacy_folder_renamed() -> None:
    assert (EVALS / "legacy_datasets" / "base.py").is_file()


def test_import_datasets_does_not_resolve_inside_evals() -> None:
    from importlib.machinery import PathFinder

    # Resolve `datasets` against evals/ alone, exactly as an evals/-first sys.path would.
    assert PathFinder.find_spec("datasets", [str(EVALS)]) is None
