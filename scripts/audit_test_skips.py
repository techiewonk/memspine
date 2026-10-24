#!/usr/bin/env python3
"""Inventory pytest skip call sites with source-only AST inspection.

This script parses Python source; it does not import project modules, evaluate
skip conditions, or infer executed skip outcomes. Explicit environment names are
context clues from source references, not proof that a skip is guarded by them.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import warnings
from pathlib import Path
from typing import Any

SCAN_DIRS = ("tests", "evals/tests", "evals/conftest.py", "docs/v2/data/test_build_plan.py")
DEFAULT_OUTPUT = "docs/provider_verification/test-skip-inventory.json"
ENV_PREFIXES = ("MEMSPINE_", "EX1A_", "HF_", "CI", "TEMP")


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def literal_value(node: ast.AST | None, constants: dict[str, Any]) -> tuple[bool, Any]:
    if node is None:
        return False, None
    if isinstance(node, ast.Name) and node.id in constants:
        return True, constants[node.id]
    try:
        return True, ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return False, None


def module_constants(tree: ast.Module) -> dict[str, Any]:
    """Resolve only direct literal module assignments; never execute source."""
    result: dict[str, Any] = {}
    for statement in tree.body:
        value_node: ast.AST | None
        if isinstance(statement, ast.Assign):
            value_node = statement.value
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            value_node = statement.value
            targets = [statement.target]
        else:
            continue
        is_literal, value = literal_value(value_node, {})
        if is_literal:
            for target in targets:
                if isinstance(target, ast.Name):
                    result[target.id] = value
    return result


def keyword_value(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def classify_call(call: ast.Call) -> tuple[str, str] | None:
    name = dotted_name(call.func)
    if name == "pytest.skip":
        return "skip", "runtime call"
    if name == "pytest.importorskip":
        return "importorskip", "import availability guard"
    if name == "pytest.mark.skipif":
        return "skipif", "conditional marker"
    if name == "pytest.mark.skip":
        return "skip", "unconditional marker"
    return None


def source_expression(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def explicit_environment_references(tree: ast.Module) -> dict[str, Any]:
    names: dict[str, set[str]] = {}
    dynamic: set[str] = set()

    def add(node: ast.AST, source: str) -> None:
        is_literal, value = literal_value(node, {})
        if is_literal and isinstance(value, str) and value.startswith(ENV_PREFIXES):
            names.setdefault(value, set()).add(source)
        else:
            dynamic.add(ast.unparse(node))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(ENV_PREFIXES)
        ):
            add(node, "module string literal (environment-like name)")
        elif isinstance(node, ast.Subscript) and dotted_name(node.value) == "os.environ":
            add(node.slice, "os.environ[...]")
        elif isinstance(node, ast.Call):
            function = dotted_name(node.func)
            if function in {"os.environ.get", "os.getenv"} and node.args:
                add(node.args[0], function)
            elif function in {"_require_env", "_configured"}:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        add(arg, f"{function}(...) argument")
                    elif not isinstance(arg, ast.Constant):
                        dynamic.add(ast.unparse(arg))

    return {
        "explicit_names": [
            {"name": name, "source_forms": sorted(forms)} for name, forms in sorted(names.items())
        ],
        "dynamic_name_expressions": sorted(dynamic),
        "interpretation": (
            "Static references only; their presence is not proof that any skip site "
            "is guarded by that variable."
        ),
    }


def owner_role(path: Path) -> dict[str, str]:
    parts = path.parts
    if len(parts) < 2 or parts[0] != "tests":
        area = "unknown test area"
        role = "test and feature maintainers"
    elif parts[1] == "unit":
        area = "/".join(parts[1:-1]) or "unit"
        role = f"feature/test maintainers for {area}"
    elif parts[1] == "integration" and len(parts) > 2 and parts[2] == "providers":
        area = "integration/providers"
        role = "provider integration maintainers"
    elif parts[1] == "integration":
        area = "integration"
        role = "integration and orchestration maintainers"
    elif parts[1] == "e2e":
        area = "e2e"
        role = "end-to-end and service-profile maintainers"
    elif parts[1] == "combinations":
        area = "combinations"
        role = "combination-matrix maintainers"
    else:
        area = "/".join(parts[1:-1]) or parts[1]
        role = f"test/feature maintainers for {area}"
    return {
        "role": role,
        "provenance": "inferred from source path; no individual owner metadata was consulted",
    }


def collect_callsites(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Inventory source sites, including aliases, fixtures and collection controls."""
    records: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    for directory in SCAN_DIRS:
        base = root / directory
        if not base.exists():
            if directory in {"evals/conftest.py", "docs/v2/data/test_build_plan.py"}:
                continue
            parse_errors.append({"file": directory, "error": "scan directory is missing"})
            continue
        for path in [base] if base.is_file() else sorted(base.rglob("*.py")):
            try:
                source = path.read_text(encoding="utf-8-sig")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(source, filename=str(path))
            except (OSError, UnicodeError, SyntaxError) as exc:
                parse_errors.append({"file": path.relative_to(root).as_posix(), "error": str(exc)})
                continue
            constants = module_constants(tree)
            env_refs = explicit_environment_references(tree)
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            relative = path.relative_to(root).as_posix()
            aliases: dict[str, str] = {}
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
                if isinstance(parent, ast.Import):
                    for alias in parent.names:
                        if alias.asname:
                            aliases[alias.asname] = alias.name
                elif isinstance(parent, ast.ImportFrom) and parent.module:
                    for alias in parent.names:
                        aliases[alias.asname or alias.name] = f"{parent.module}.{alias.name}"

            def canonical(node: ast.AST, aliases: dict[str, str] = aliases) -> str:
                name = dotted_name(node) or ""
                head, _, tail = name.partition(".")
                return aliases.get(head, head) + (f".{tail}" if tail else "")

            def scope(node: ast.AST, parents: dict[ast.AST, ast.AST] = parents) -> tuple[str, str]:
                names: list[str] = []
                context = "module"
                current = node
                while current in parents:
                    current = parents[current]
                    if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.append(current.name)
                        if context == "module" and not isinstance(current, ast.ClassDef):
                            decorators = [
                                canonical(d.func if isinstance(d, ast.Call) else d)
                                for d in current.decorator_list
                            ]
                            context = (
                                "collection_hook"
                                if current.name.startswith("pytest_")
                                else "fixture"
                                if "pytest.fixture" in decorators
                                else "test"
                                if current.name.startswith("test_")
                                else "helper"
                            )
                return ".".join(reversed(names)) or "<module>", context

            for node in ast.walk(tree):
                kind: str | None = None
                form = ""
                target_node: ast.AST | None = None
                reason_node: ast.AST | None = None
                name = ""
                parent_node = parents.get(node)
                if isinstance(node, ast.Call):
                    name = canonical(node.func)
                    forms = {
                        "pytest.skip": ("skip", "runtime call"),
                        "pytest.importorskip": ("importorskip", "import availability guard"),
                        "pytest.mark.skipif": ("skipif", "conditional marker"),
                        "pytest.mark.skip": ("skip", "unconditional marker"),
                        "pytest.mark.xfail": ("xfail", "expected failure marker, not a skip"),
                        "pytest.xfail": ("xfail", "expected failure call, not a skip"),
                        "unittest.skip": ("skip", "unittest unconditional marker"),
                        "unittest.skipIf": ("skipif", "unittest conditional marker"),
                        "unittest.skipUnless": ("skipif", "unittest inverse conditional marker"),
                    }
                    match = forms.get(name)
                    if name == "self.skipTest":
                        ancestor: ast.AST = node
                        while ancestor in parents:
                            ancestor = parents[ancestor]
                            if isinstance(ancestor, ast.ClassDef) and any(
                                canonical(base) == "unittest.TestCase" for base in ancestor.bases
                            ):
                                match = ("skip", "unittest TestCase runtime skip")
                                break
                    if match:
                        kind, form = match
                        if kind in {"skipif", "importorskip"}:
                            target_node = node.args[0] if node.args else None
                            reason_node = keyword_value(node, "reason")
                            if name.startswith("unittest.") and len(node.args) > 1:
                                reason_node = node.args[1]
                        else:
                            reason_node = (
                                node.args[0] if node.args else keyword_value(node, "reason")
                            )
                    elif name.endswith(".add_marker"):
                        kind, form = (
                            "collection_control",
                            "dynamic marker attachment; not necessarily a skip",
                        )
                    elif name in {"pytest.param"}:
                        # Its nested marker is inventoried separately.
                        continue
                elif (
                    isinstance(node, ast.Attribute)
                    and canonical(node) in {"pytest.mark.skip", "pytest.mark.xfail"}
                    and not (isinstance(parent_node, ast.Call) and parent_node.func is node)
                ):
                    kind = "skip" if canonical(node).endswith(".skip") else "xfail"
                    form = "bare marker"
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if any(
                        isinstance(t, ast.Name)
                        and t.id in {"collect_ignore", "collect_ignore_glob"}
                        for t in targets
                    ):
                        kind, form = "collection_control", "collection exclusion assignment"
                        target_node = node.value
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                    "pytest_collection_modifyitems",
                    "pytest_ignore_collect",
                    "pytest_collection",
                }:
                    kind, form = "collection_control", "collection hook definition"
                if kind is None:
                    continue
                is_literal, value = literal_value(reason_node, constants)
                is_target_literal, target_value = literal_value(target_node, constants)
                node_name, context = scope(node)
                reason_status = (
                    "literal_or_module_constant"
                    if is_literal and value
                    else "dynamic_expression"
                    if reason_node is not None
                    else "implicit_import_unavailable_reason"
                    if kind == "importorskip"
                    else "not_applicable"
                    if kind == "collection_control"
                    else "reason_not_supplied"
                )
                line = getattr(node, "lineno", 0)
                records.append(
                    {
                        "id": f"{relative}:{line}:{kind}",
                        "file": relative,
                        "line": line,
                        "kind": kind,
                        "form": form,
                        "node": node_name,
                        "context": context,
                        "category": (
                            "optional_import"
                            if kind == "importorskip"
                            else "conditional_skip"
                            if kind == "skipif"
                            else "expected_failure_not_skip"
                            if kind == "xfail"
                            else "collection_control"
                            if kind == "collection_control"
                            else "runtime_or_unconditional_skip"
                        ),
                        "condition_expression": source_expression(target_node)
                        if kind in {"skipif", "collection_control"}
                        else None,
                        "condition_known_literal": target_value if is_target_literal else None,
                        "reason_expression": source_expression(reason_node),
                        "reason_known_literal": value if is_literal else None,
                        "reason_status": reason_status,
                        "import_target_expression": source_expression(target_node)
                        if kind == "importorskip"
                        else None,
                        "import_target_known_literal": target_value if is_target_literal else None,
                        "module_env_refs": env_refs,
                        "owner_role": owner_role(Path(relative)),
                        "source_sha256": digest,
                    }
                )
    records.sort(key=lambda record: (record["file"], record["line"], record["kind"]))
    return records, parse_errors


def historical_aggregates(root: Path) -> list[dict[str, Any]]:
    """Extract retained aggregate summaries without attributing individual skips."""
    sources = [
        Path("docs/provider_verification/gliner25-adapters-tests.log"),
        Path("docs/provider_verification/temporal-migration-regression.log"),
        Path("docs/provider_verification/README.md"),
    ]
    summary = re.compile(r"\b\d[\d,]* passed\b.*\b\d[\d,]* skipped\b", re.IGNORECASE)
    results: list[dict[str, Any]] = []
    for relative in sources:
        path = root / relative
        if not path.exists():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not summary.search(line):
                continue
            results.append(
                {
                    "source": relative.as_posix(),
                    "line": line_number,
                    "aggregate_text": line.strip(),
                    "individual_skip_attribution": "unattributed_aggregate",
                    "reason": "retained source contains aggregate skipped count without node IDs/reasons",
                }
            )
    return results


def apply_dispositions(records: list[dict[str, Any]], metadata: dict[str, Any]) -> list[str]:
    """Attach reviewed ownership separately from source-inferred facts."""
    missing: list[str] = []
    categories = metadata.get("categories", {})
    sites = metadata.get("sites", {})
    for record in records:
        site = sites.get(record["id"], {})
        category = site.get("category", record["category"])
        review = {**categories.get(category, {}), **site}
        record["review"] = review
        required = ("reason", "owner_role", "next_milestone", "disposition", "review_date")
        if (
            not site
            or not all(review.get(key) for key in required)
            or "prerequisites" not in site
            or site.get("source_sha256") != record["source_sha256"]
        ):
            missing.append(record["id"])
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--dispositions",
        type=Path,
        default=None,
        help="Optional reviewed category/site metadata; incomplete reviews fail the audit",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    output = (args.output or (root / DEFAULT_OUTPUT)).resolve()

    records, parse_errors = collect_callsites(root)
    missing_reviews: list[str] = []
    stale_review_sites: list[str] = []
    if args.dispositions is not None:
        metadata = json.loads(args.dispositions.read_text(encoding="utf-8"))
        missing_reviews = apply_dispositions(records, metadata)
        current_ids = {record["id"] for record in records}
        stale_review_sites = sorted(set(metadata.get("sites", {})) - current_ids)
    counts: dict[str, int] = {}
    for record in records:
        counts[record["kind"]] = counts.get(record["kind"], 0) + 1
    result = {
        "schema_version": 2,
        "method": "stdlib ast.parse; source is parsed but never imported or evaluated",
        "scan_directories": list(SCAN_DIRS),
        "callsite_count": len(records),
        "coverage_limits": [
            "Source sites are not executed skip counts; parametrization and module guards can affect multiple tests.",
            "No imports, condition evaluation, external plugins, generated code or environment probes are executed.",
            "Direct pytest/unittest calls and import aliases are resolved; arbitrary runtime aliases and helper-generated markers require manual review.",
            "xfail is recorded separately by kind and does not imply skipped coverage.",
            "Historical docs/combined-memory-locomo-eval-v4 source snapshot tests are excluded: third-party archived engine, not active MemSpine test coverage.",
        ],
        "missing_reason_sites": [
            r["id"] for r in records if r["reason_status"] == "reason_not_supplied"
        ],
        "callsite_counts_by_kind": dict(sorted(counts.items())),
        "owner_role_note": "Roles are inferred from source paths, not repository ownership metadata.",
        "environment_reference_note": (
            "Explicit names and dynamic expressions are source references only; they do not prove "
            "a relationship to a skip condition."
        ),
        "historical_execution_aggregates": historical_aggregates(root),
        "parse_errors": parse_errors,
        "missing_reviews": missing_reviews,
        "stale_review_sites": stale_review_sites,
        "review_metadata_supplied": args.dispositions is not None,
        "callsites": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(records)} static skip call sites to {output}")
    print(f"Kinds: {json.dumps(dict(sorted(counts.items())), sort_keys=True)}")
    print(f"Parse errors: {len(parse_errors)}")
    return 1 if parse_errors or missing_reviews or stale_review_sites else 0


if __name__ == "__main__":
    raise SystemExit(main())
