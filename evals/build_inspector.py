"""TI-4: build the self-contained Trace Inspector page (inline JS/CSS, data embedded, no network).

    python evals/build_inspector.py <bundle_dir>                       # bundle from memspine_evals.inspect.assemble
    python evals/build_inspector.py --run <run> --data <ds|demo> [--compare <run2>] [--out <dir>]
    python evals/build_inspector.py --demo                             # invented conversation only

Output: ``<bundle_dir>/index.html`` (default dir ``evals/runs/_analysis/inspect/<run>/``, gitignored: real runs hold
benchmark text). Views: Conversation, Window, Record & links, Question, Compare, Checks. Large conversations render
lazily (sessions on open, turns in chunks of 60).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from memspine_evals.inspect.assemble import FILES, assemble, load_jsonl

TEMPLATE = HERE / "memspine_evals" / "inspect" / "inspector_template.html"
ANALYSIS = HERE / "runs" / "_analysis" / "inspect"


def load_bundle(d: Path) -> dict:
    data = {n: load_jsonl(d / f"{n}.jsonl") for n in FILES}
    data["manifest"] = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    cmp = d / "compare.json"
    data["compare"] = json.loads(cmp.read_text(encoding="utf-8")) if cmp.exists() else None
    return data


def embed(data: dict) -> str:
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    bs = chr(92)
    return (
        s.replace("</", "<" + bs + "/")
        .replace("<!--", "<" + bs + "!--")
        .replace(" ", bs + "u2028")
        .replace(" ", bs + "u2029")
    )


def build(bundle_dir: Path, out: Path | None = None) -> Path:
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", embed(load_bundle(bundle_dir)))
    out = out or bundle_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def build_demo(dest: Path | None = None) -> Path:
    from memspine_evals.inspect.demo_run import make_demo_run

    dest = dest or ANALYSIS / "demo"
    with tempfile.TemporaryDirectory() as t:
        a = make_demo_run(t, "v2")
        b = make_demo_run(t, "ref")
        assemble(a, dest, "demo", compare_with=b)
    return build(dest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bundle", nargs="?", help="an assembled bundle directory")
    ap.add_argument("--run")
    ap.add_argument("--data")
    ap.add_argument("--compare")
    ap.add_argument("--out")
    ap.add_argument("--no-content", action="store_true")
    ap.add_argument(
        "--demo",
        action="store_true",
        help="build evals/runs/_analysis/inspect/demo/index.html from the invented fixture",
    )
    a = ap.parse_args(argv)
    if a.demo:
        p = build_demo(Path(a.out) if a.out else None)
    elif a.run:
        from memspine_evals.inspect.assemble import resolve_run

        out = Path(a.out) if a.out else ANALYSIS / resolve_run(a.run)["run_id"]
        man = assemble(a.run, out, a.data, content=not a.no_content, compare_with=a.compare)
        print(man["counts"])
        p = build(out)
    elif a.bundle:
        p = build(Path(a.bundle))
    else:
        ap.error("give a bundle directory, --run, or --demo")
    print(f"{p} ({p.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
