"""CI gate (X-02): exactly one Alembic head per migration tree, no unmerged branch.

Parses the ``revision`` / ``down_revision`` assignments of every ``versions/*.py`` under any
``alembic`` directory below ``src/`` (no alembic import, so it runs anywhere). A merge
revision lists several parents in a tuple; a branch that a merge revision joins again is
fine, a branch left open shows up as a second head.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def version_dirs(src: Path = SRC) -> list[Path]:
    return sorted(p for p in src.rglob("versions") if p.is_dir() and p.parent.name == "alembic")


def _assigned(tree: ast.Module, name: str):
    for node in tree.body:
        value = None
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        ) or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            value = node.value
        if value is not None:
            return ast.literal_eval(value)
    raise KeyError(name)


def read_graph(vdir: Path) -> dict[str, tuple[str, ...]]:
    graph: dict[str, tuple[str, ...]] = {}
    for f in sorted(vdir.glob("*.py")):
        if f.name == "__init__.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        rev = _assigned(tree, "revision")
        down = _assigned(tree, "down_revision")
        parents = () if down is None else (down,) if isinstance(down, str) else tuple(down)
        assert rev not in graph, f"{f}: duplicate revision id {rev}"
        graph[rev] = parents
    return graph


def problems(graph: dict[str, tuple[str, ...]]) -> list[str]:
    out: list[str] = []
    if not graph:
        return ["no revisions found"]
    for rev, parents in graph.items():
        for p in parents:
            if p not in graph:
                out.append(f"{rev}: down_revision {p} does not exist")
    children: dict[str, list[str]] = {}
    for rev, parents in graph.items():
        for p in parents:
            children.setdefault(p, []).append(rev)
    heads = sorted(r for r in graph if r not in children)
    if len(heads) != 1:
        branches = {p: sorted(k) for p, k in children.items() if len(k) > 1}
        out.append(
            f"expected exactly one head, found {len(heads)}: {heads}"
            f" (branch points: {branches or 'none'}; add a merge revision)"
        )
    roots = sorted(r for r, ps in graph.items() if not ps)
    if len(roots) != 1:
        out.append(f"expected exactly one base revision, found {roots}")
    return out


def test_at_least_one_alembic_tree_exists():
    assert version_dirs(), "no alembic versions directory under src/"


def test_each_alembic_tree_has_a_single_head():
    for vdir in version_dirs():
        errs = problems(read_graph(vdir))
        assert not errs, f"{vdir.relative_to(REPO)}:\n  " + "\n  ".join(errs)


# --- checker self-tests ---------------------------------------------------------------


def test_linear_chain_passes():
    assert not problems({"1": (), "2": ("1",), "3": ("2",)})


def test_unmerged_branch_fails():
    errs = problems({"1": (), "2": ("1",), "3": ("1",)})
    assert any("one head" in e and "branch points" in e for e in errs)


def test_merged_branch_passes():
    assert not problems({"1": (), "2": ("1",), "3": ("1",), "4": ("2", "3")})


def test_missing_parent_fails():
    assert any("does not exist" in e for e in problems({"1": (), "2": ("9",)}))


def test_two_bases_fail():
    assert any("base revision" in e for e in problems({"1": (), "2": (), "3": ("1", "2")}))


def test_real_tree_graph_reads():
    for vdir in version_dirs():
        assert read_graph(vdir)
