# ruff: noqa: E501, SIM102, SIM905, B023, RUF100
"""TI-8: build the stage -> parameter catalogue.

    python scripts/build_stage_params.py            # write docs/v2/data/stage_params.json + docs/v2/STAGE_PARAMS.md
    python scripts/build_stage_params.py --check    # exit 1 if either file is out of date

Inputs (all read-only): the pydantic config schema, ``docs/USAGE.md`` (descriptions),
``docs/v2/PIPELINE_STAGE_INVENTORY.md`` (stage ids and file:function), the engine source
(AST scan for config attribute reads), ``evals/v2_queue.tsv`` + ``docs/v2/SCREENS.md`` (screens),
``src/memspine/config/presets`` (profile screens), the prompt files, ``evals/memspine_evals/cli.py``
and ``readers.py`` / ``judge_prompts.py`` (harness flags and prompts), plus the hand-curated
``docs/v2/data/stage_params_manual.json`` for what the scan cannot attribute.

How a key is attributed to a stage: every ``<expr>.<leaf>`` attribute access (and ``getattr(x,
"leaf")``) in ``src/memspine`` outside ``config/`` is a read site of the schema keys whose last
segment is ``leaf``. A leaf name that exists under several sections (``enabled``, ``model``) only
counts when the access chain names the section (``cfg.read.enabled``, ``self._read.enabled``).
The read site (file, enclosing function) is then matched to the inventory: a function named in the
stage's file:function cell scores 10, a file named there scores 3. The best stages win.

No dataset text is read or written; harness prompts are recorded by id and content hash only.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import sys
import typing
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "evals"))

OUT_JSON = REPO / "docs" / "v2" / "data" / "stage_params.json"
OUT_MD = REPO / "docs" / "v2" / "STAGE_PARAMS.md"
MANUAL = REPO / "docs" / "v2" / "data" / "stage_params_manual.json"
INVENTORY = REPO / "docs" / "v2" / "PIPELINE_STAGE_INVENTORY.md"
REGISTRY = REPO / "docs" / "v2" / "data" / "stage_trace_registry.json"
USAGE = REPO / "docs" / "USAGE.md"
SRC = REPO / "src" / "memspine"
QUEUE = REPO / "evals" / "v2_queue.tsv"
SCREENS_MD = REPO / "docs" / "v2" / "SCREENS.md"
PRESETS = SRC / "config" / "presets"
PROMPTS = SRC / "prompts" / "defaults"
CLI = REPO / "evals" / "memspine_evals" / "cli.py"

WILDCARD = "*"
HARNESS_STAGE = "H0"
HARNESS_NAME = "Eval harness run control"
MAX_STAGES_PER_KEY = 4


# --------------------------------------------------------------------------- schema keys


def _is_model(a: object) -> bool:
    from pydantic import BaseModel

    return isinstance(a, type) and issubclass(a, BaseModel)


def _dict_value_type(a: object) -> object | None:
    if typing.get_origin(a) is dict:
        args = typing.get_args(a)
        if len(args) == 2:
            return args[1]
    return None


def _type_str(a: object) -> str:
    s = str(a) if typing.get_origin(a) is not None else getattr(a, "__name__", str(a))
    s = s.replace("typing.", "").replace("<class '", "").replace("'>", "")
    return re.sub(r"\bmemspine\.[\w.]*\.(\w+)", r"\1", s)


def _constraints(field) -> dict:
    out: dict = {}
    for m in field.metadata:
        for attr in ("ge", "gt", "le", "lt", "min_length", "max_length"):
            v = getattr(m, attr, None)
            if v is not None:
                out[attr] = v
    return out


def _literal_choices(a: object) -> list | None:
    args = [a] if typing.get_origin(a) is typing.Literal else list(typing.get_args(a))
    for x in args:
        if typing.get_origin(x) is typing.Literal:
            return list(typing.get_args(x))
    return None


def _default(field):
    from pydantic_core import PydanticUndefined

    if field.default is not PydanticUndefined:
        d = field.default
    elif field.default_factory is not None:
        try:
            d = field.default_factory()
        except Exception:
            return None
    else:
        return None
    if hasattr(d, "model_dump"):
        d = d.model_dump(mode="json")
    return json.loads(json.dumps(d, default=str, sort_keys=True))


def schema_keys() -> dict[str, dict]:
    """key path -> {type, default, choices|range}; mirrors tests/unit/test_docs_config_surface.py."""
    from memspine.config.schema import MemspineConfig

    out: dict[str, dict] = {}

    def walk(model, prefix: str) -> None:
        for name, f in model.model_fields.items():
            a = f.annotation
            key = f"{prefix}{name}"
            if _is_model(a):
                walk(a, key + ".")
                continue
            vt = _dict_value_type(a)
            if vt is not None and _is_model(vt):
                walk(vt, f"{key}.{WILDCARD}.")
                continue
            info: dict = {"type": _type_str(a), "default": _default(f)}
            ch = _literal_choices(a)
            if ch is not None:
                info["choices"] = ch
            rng = _constraints(f)
            if rng:
                info["range"] = rng
            out[key] = info

    walk(MemspineConfig, "")
    return out


def usage_descriptions() -> dict[str, str]:
    text = USAGE.read_text(encoding="utf-8")
    start, end = "<!-- CONFIG-KEYS-TABLE:START -->", "<!-- CONFIG-KEYS-TABLE:END -->"
    region = text.split(start, 1)[1].split(end, 1)[0]
    out: dict[str, str] = {}
    for line in region.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        m = re.search(r"`([^`]+)`", cells[0]) if cells else None
        if m and len(cells) >= 3:
            # a row with an empty default cell is malformed (shifted cells): use the last cell
            out[m.group(1)] = cells[-1] if cells[1] == "" else " | ".join(cells[2:])
    return out


_ID_PREFIX = re.compile(
    r"^[A-Z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*(?: v\d+)?(?: ?/ ?[A-Z][-A-Za-z0-9]*(?: v\d+)?)*"
    r"(?:, with `[^`]*`)?(?: \([^)]*\))?[.:,]?\s+"
)


def effect_line(desc: str) -> str:
    """First sentence of a USAGE.md description, without its leading work-package id."""
    d = re.sub(r"\s+", " ", desc).strip()
    m = _ID_PREFIX.match(d)
    if m and re.search(r"\d", m.group(0)) and len(d) > m.end() + 10:
        d = d[m.end() :].lstrip(". ")
        d = d[:1].upper() + d[1:] if d[:1].isalpha() else d
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z`])", d, maxsplit=1)
    s = parts[0]
    if len(s) < 25 and len(parts) > 1:
        s = d[: len(s) + 1 + len(re.split(r"(?<=[.;])\s+(?=[A-Z`])", parts[1], maxsplit=1)[0])]
    return s[:240].rstrip()


# --------------------------------------------------------------------------- inventory


_STOP = frozenset(
    "read write memory record records stage default true false when with that this from into used uses only "
    "each config keys value values mode path flag turn turns text step steps items item list query "
    "than then them they will also does have been must never after before above below under over "
    "off".split()
)


def _word_tokens(text: str) -> set[str]:
    return {
        w
        for w in re.split(r"[^a-z0-9]+", text.lower().replace("_", " "))
        if len(w) >= 3 and w not in _STOP and not w.isdigit()
    }


def _overlap(a: set[str], b: set[str]) -> int:
    n = 0
    for x in a:
        if any(x == y or (len(x) >= 5 and len(y) >= 5 and x[:5] == y[:5]) for y in b):
            n += 1
    return n


class Stage:
    def __init__(self, sid: str, name: str, ff: str, text: str):
        self.id = sid
        self.name = name
        self.file_function = ff
        self.text = text.lower()
        self.files = [p for p in re.findall(r"[\w/]+\.py", ff) if "…" not in p]
        self.words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", re.sub(r"\.py\b", "", ff)))
        self.tokens = _word_tokens(f"{name} {ff} {text}")


def parse_inventory() -> dict[str, Stage]:
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))["stages"]
    out: dict[str, Stage] = {}
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| ([ABC]\d+[a-z]?) \|", line)
        if not m:
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        sid = cells[0]
        name = re.sub(r"\*\*", "", cells[1])
        ff = cells[2]
        events = " ".join(reg.get(sid, {}).get("events", []))
        out[sid] = Stage(sid, name, ff, f"{name} {ff} {events}")
    return out


# --------------------------------------------------------------------------- source scan


class Hit(typing.NamedTuple):
    path: str  # repo-relative, posix
    func: tuple  # enclosing qualname parts
    line: int
    chain: tuple  # attribute chain tokens before the leaf (outermost last)


class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str, leaves: set[str]):
        self.rel = rel
        self.leaves = leaves
        self.stack: list[str] = []
        self.hits: dict[str, list[Hit]] = defaultdict(list)
        self.defs: set[str] = set()
        self.classes: set[str] = set()

    def _scope(self, node) -> None:
        self.defs.add(node.name)
        if isinstance(node, ast.ClassDef):
            self.classes.add(node.name)
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope  # type: ignore[assignment]

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self.leaves:
            chain: list[str] = []
            v = node.value
            while isinstance(v, ast.Attribute):
                chain.append(v.attr)
                v = v.value
            if isinstance(v, ast.Name):
                chain.append(v.id)
            self.hits[node.attr].append(Hit(self.rel, tuple(self.stack), node.lineno, tuple(chain)))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if (
            isinstance(f, ast.Name)
            and f.id in ("getattr", "hasattr", "setattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in self.leaves
        ):
            tgt = node.args[0]
            chain = []
            while isinstance(tgt, ast.Attribute):
                chain.append(tgt.attr)
                tgt = tgt.value
            if isinstance(tgt, ast.Name):
                chain.append(tgt.id)
            self.hits[node.args[1].value].append(
                Hit(self.rel, tuple(self.stack), node.lineno, tuple(chain))
            )
        self.generic_visit(node)


def scan_source(leaves: set[str]) -> tuple[dict[str, list[Hit]], dict[str, set[str]]]:
    hits: dict[str, list[Hit]] = defaultdict(list)
    defs_in: dict[str, set[str]] = defaultdict(set)  # def name -> files
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if "/config/" in rel or "/__pycache__/" in rel:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        sc = _Scanner(rel, leaves)
        sc.visit(tree)
        for k, v in sc.hits.items():
            hits[k].extend(v)
        for d in sc.defs:
            defs_in[d].add(rel)
        defs_in["__classes__"] |= sc.classes
    return hits, defs_in


def _norm_tok(t: str) -> str:
    t = t.lstrip("_").lower()
    for suf in ("_cfg", "_config", "_conf", "cfg", "config", "_policy"):
        if t.endswith(suf) and len(t) > len(suf):
            t = t[: -len(suf)]
    return t


def hit_matches_key(hit: Hit, key: str, ambiguous: bool) -> bool:
    if not ambiguous:
        return True
    segs = [s for s in key.split(".") if s != WILDCARD]
    parent = segs[-2] if len(segs) >= 2 else ""
    top = segs[0]
    toks = [_norm_tok(t) for t in hit.chain]
    if toks and toks[0] == parent:  # chain[0] is the token directly before the leaf
        return True
    # a longer chain (cfg.read.enabled) names the section anywhere; the top section counts
    # only when the parent token is not some other sub-model's name
    return top in toks[1:] or (len(segs) == 2 and top in toks)


#: files that wire every stage together; a file-only match there says nothing about the stage
_WIRING_FILES = {"src/memspine/api/engine.py", "src/memspine/runtime/container.py"}
_GENERIC_STEMS = {"engine", "router", "request", "state", "base", "api"}


def site_stage_scores(
    hit: Hit, stages: dict[str, Stage], defs_in: dict[str, set[str]], extra: str = ""
) -> dict[str, int]:
    scores: dict[str, int] = {}
    classes = defs_in.get("__classes__", set())
    parts = {p for p in hit.func if p not in classes} or set(hit.func)
    stem = Path(hit.path).stem
    for st in stages.values():
        sc = 0
        for fn in parts & st.words:
            n_files = len(defs_in.get(fn, ()))
            if (
                n_files <= 3
                or stem in st.words
                or any(f.endswith(Path(hit.path).name) for f in st.files)
            ):
                if len(fn) >= 4:
                    sc = max(sc, 10)
        if (
            sc == 0
            and hit.path not in _WIRING_FILES
            and any(hit.path.endswith(f) for f in st.files)
        ):
            sc = 3
        elif sc == 0 and stem in st.words and stem not in _GENERIC_STEMS:
            sc = 1
        if sc:
            ov = _overlap(_word_tokens(" ".join(hit.func) + " " + extra), st.tokens)
            if sc == 1 and ov == 0:
                continue
            scores[st.id] = sc + 2 * ov
    return scores


# --------------------------------------------------------------------------- screens


def _tokens(text: str) -> list[str]:
    return re.findall(r"\b([a-z][a-z_0-9]*(?:\.[a-z_0-9*]+)+)\b", text)


def _preset_keys(name: str) -> set[str]:
    import yaml

    p = PRESETS / f"{name}.yaml"
    if not p.exists():
        return set()
    out: set[str] = set()

    def walk(d, pre):
        if isinstance(d, dict) and d:
            for k, v in d.items():
                walk(v, f"{pre}{k}.")
        else:
            out.add(pre[:-1])

    walk(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, "")
    return out


def screen_map() -> dict[str, set[str]]:
    """screen id -> dotted tokens (config keys, or `cli:--flag`) it varied."""
    rows: dict[str, str] = {}
    for line in QUEUE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        c = line.split("\t")
        if len(c) >= 11 and re.match(r"^S\d+$", c[0]):
            rows[c[0]] = c[10]
    for line in SCREENS_MD.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| (S\d+) \|", line)
        if m:
            cells = [x.strip().strip("`") for x in line.split("|")[1:-1]]
            if len(cells) > 3 and m.group(1) not in rows:
                rows[m.group(1)] = cells[3]
    out: dict[str, set[str]] = {}
    for sid, flag in rows.items():
        toks = set(_tokens(flag))
        for pm in re.finditer(r"profile ([a-z0-9-]+)", flag):
            toks |= _preset_keys(pm.group(1))
        toks |= {f"cli:{f}" for f in re.findall(r"(--[a-z][a-z0-9-]+)", flag)}
        out[sid] = toks
    return out


def screens_for(key: str, smap: dict[str, set[str]]) -> list[str]:
    hit = []
    for sid, toks in smap.items():
        for t in toks:
            if t == key or key.startswith(t + ".") or (key.replace(f".{WILDCARD}.", ".") == t):
                hit.append(sid)
                break
    return sorted(set(hit), key=lambda s: int(s[1:]))


# --------------------------------------------------------------------------- prompts


def prompt_files() -> list[dict]:
    out = []
    for p in sorted(PROMPTS.rglob("*.yaml")):
        head = p.read_text(encoding="utf-8").splitlines()[:12]
        meta = {}
        for ln in head:
            m = re.match(r"^(id|version|role):\s*(\S+)", ln)
            if m:
                meta[m.group(1)] = m.group(2)
        rel = p.relative_to(REPO).as_posix()
        out.append(
            {
                "prompt_id": meta.get("id", p.stem),
                "role": meta.get("role", ""),
                "file": rel,
                "version": meta.get("version", ""),
                "_stem": p.stem,
            }
        )
    return out


def string_sites(names: set[str]) -> dict[str, list[Hit]]:
    """Call sites where a prompt id / role appears as a string literal (outside prompts/, config/)."""
    hits: dict[str, list[Hit]] = defaultdict(list)
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if any(s in rel for s in ("/config/", "/prompts/", "/__pycache__/")):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        class V(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def _s(self, n):
                self.stack.append(n.name)
                self.generic_visit(n)
                self.stack.pop()

            visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _s  # type: ignore

            def visit_Constant(self, n):
                if isinstance(n.value, str) and n.value in names:
                    hits[n.value].append(Hit(rel, tuple(self.stack), n.lineno, ()))

        V().visit(tree)
    return hits


def harness_prompts() -> dict[str, list[dict]]:
    """C32 reader prompts and C34 judge prompts: id + content hash only (no text)."""
    out: dict[str, list[dict]] = {"C32": [], "C34": []}
    from memspine_evals import judge_prompts, readers  # noqa: PLC0415

    def sha(t: str) -> str:
        return hashlib.sha256(t.encode("utf-8")).hexdigest()[:8]

    for name, text in sorted(readers.QA_PROMPTS.items()):
        out["C32"].append(
            {
                "prompt_id": f"qa:{name}",
                "role": "reader",
                "file": "evals/memspine_evals/readers.py",
                "version": f"sha:{sha(str(text))}",
            }
        )
    for name, rp in sorted(readers.ROUTED_QA_PROMPTS.items()):
        out["C32"].append(
            {
                "prompt_id": f"qa:{name}",
                "role": "reader (routed by question shape)",
                "file": "evals/memspine_evals/readers.py",
                "version": str(rp.router_version),
            }
        )
    for pid, jp in sorted(judge_prompts.JUDGE_PROMPTS.items()):
        out["C34"].append(
            {
                "prompt_id": f"judge:{pid}",
                "role": "judge",
                "file": "evals/memspine_evals/judge_prompts.py",
                "version": f"sha:{sha(str(getattr(jp, 'text', pid)))}",
            }
        )
    return out


# --------------------------------------------------------------------------- CLI flags

CLI_STAGE_RULES: list[tuple[str, str]] = [
    (
        r"^--(count-verify|date-repair|duration-solve|milestones|verify-slots|evidence-table|assertion-check|verify-answer|repair-evidence)",
        "C33",
    ),
    (r"^--(retry-|refusal-match|no-record-hint|premise-tolerant)", "C33"),
    (r"^--(judge-|judge$)", "C34"),
    (
        r"^--(reader-model|qa-prompt|no-memory-prompt|stamp-timezone|presence-penalty|frequency-penalty|top-p|sampler-seed|server-ctx|strict-ctx|token-count|tokenizer-id|cache-reader|base-url)",
        "C32",
    ),
    (r"^--decider", "C5"),
    (
        r"^--(budget|top-k|dense|hybrid|memspine-read-mode|memspine-mark-hits|memspine-context-order|memspine-as-of-question-date|matched-budget-tokens|naive-dense-same-embedder|retrieval-only)",
        "C31",
    ),
    (r"^--memspine-batch-turns", "A1"),
    (r"^--memspine-build-sleep", "B0"),
]


def cli_flags() -> list[dict]:
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    seen: dict[str, dict] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument":
            names = [
                a.value
                for a in n.args
                if isinstance(a, ast.Constant) and str(a.value).startswith("--")
            ]
            if not names or names[0] in seen:
                continue
            kw = {k.arg: k.value for k in n.keywords}

            def lit(node):
                if node is None:
                    return None
                try:
                    return ast.literal_eval(node)
                except Exception:
                    return ast.unparse(node)

            action = lit(kw.get("action"))
            typ = (
                ast.unparse(kw["type"])
                if "type" in kw
                else ("bool" if action in ("store_true", "store_false") else "str")
            )
            default = lit(kw.get("default"))
            if action == "store_true":
                default = False
            choices = lit(kw.get("choices"))
            help_ = lit(kw.get("help")) or ""
            seen[names[0]] = {
                "flag": names[0],
                "type": typ,
                "default": default
                if isinstance(default, (str, int, float, bool, type(None)))
                else str(default),
                "choices": list(choices)
                if isinstance(choices, (list, tuple))
                else (choices if choices else None),
                "help": re.sub(r"\s+", " ", str(help_)).strip(),
            }
    return list(seen.values())


# --------------------------------------------------------------------------- rebuild scope


def rebuild_scope(key: str) -> str:
    k = key
    if k.startswith(("embedding.", "storage.", "deployment.data_dir", "event_log.", "graph.")):
        return "index" if not k.startswith("storage.cache.") else ""
    if k.startswith(("write.", "ingest.", "data_shape.")):
        return "ingest"
    if k.startswith("offline."):
        return "sleep"
    if re.match(r"^read\.(lexical_(analyzer|dates)|index_|window_index)", k):
        return "index"
    return ""


# --------------------------------------------------------------------------- build


def build() -> dict:
    keys = schema_keys()
    desc = usage_descriptions()
    stages = parse_inventory()
    manual = json.loads(MANUAL.read_text(encoding="utf-8")) if MANUAL.exists() else {}
    smap = screen_map()

    leaf_count: dict[str, int] = defaultdict(int)
    for k in keys:
        leaf_count[k.rsplit(".", 1)[-1]] += 1
    leaves = set(leaf_count)
    hits, defs_in = scan_source(leaves)

    key_stages: dict[str, dict[str, int]] = {}
    for key in keys:
        leaf = key.rsplit(".", 1)[-1]
        amb = leaf_count[leaf] > 1
        agg: dict[str, int] = defaultdict(int)
        for h in hits.get(leaf, ()):
            if not hit_matches_key(h, key, amb):
                continue
            sc = site_stage_scores(h, stages, defs_in, leaf + " " + effect_line(desc.get(key, "")))
            if not sc:
                continue
            best = max(sc.values())
            for sid, v in sc.items():
                if v == best:
                    agg[sid] += v
        if agg:
            ranked = sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))
            top = ranked[0][1]
            key_stages[key] = {a: b for a, b in ranked if b * 2 >= top}
            key_stages[key] = dict(list(key_stages[key].items())[:MAX_STAGES_PER_KEY])

    # manual attribution: authoritative replace, then additive, then removal
    for pat, sids in manual.get("set_key_stages", {}).items():
        for key in keys:
            if fnmatch.fnmatchcase(key, pat):
                key_stages[key] = {s: 100 for s in sids}
    man_map: dict[str, list[str]] = manual.get("key_to_stages", {})
    for pat, sids in man_map.items():
        for key in keys:
            if fnmatch.fnmatchcase(key, pat):
                d = key_stages.setdefault(key, {})
                for s in sids:
                    d.setdefault(s, 100)
    man_drop: dict[str, list[str]] = manual.get("remove_key_stage", {})
    for pat, sids in man_drop.items():
        for key in keys:
            if fnmatch.fnmatchcase(key, pat) and key in key_stages:
                kept = {a: b for a, b in key_stages[key].items() if a not in sids}
                if kept:  # never strip a key of its last stage; fix those in set_key_stages
                    key_stages[key] = kept
    man_unattr: dict[str, str] = manual.get("unattributed", {})
    exact_set = {p for p in manual.get("set_key_stages", {}) if not re.search(r"[*?\[]", p)}
    for pat in manual.get("cross_cutting", {}):
        for key in keys:
            if fnmatch.fnmatchcase(key, pat) and key not in exact_set:
                key_stages.pop(key, None)
    man_unattr = {**man_unattr, **manual.get("cross_cutting", {})}
    unattributed = {}
    for key in sorted(keys):
        if key in key_stages:
            continue
        reason = next((r for pat, r in man_unattr.items() if fnmatch.fnmatchcase(key, pat)), None)
        unattributed[key] = reason or "no read site matched an inventory stage"

    out_stages: dict[str, dict] = {}
    for sid, st in stages.items():
        out_stages[sid] = {
            "name": st.name,
            "file_function": st.file_function,
            "keys": [],
            "prompts": [],
            "models": [],
        }
    out_stages[HARNESS_STAGE] = {
        "name": HARNESS_NAME,
        "file_function": "evals/memspine_evals/cli.py:run (argparse)",
        "keys": [],
        "prompts": [],
        "models": [],
    }

    for key, sc in sorted(key_stages.items()):
        info = keys[key]
        d = desc.get(key, "")
        entry = {
            "key": key,
            "default": info["default"],
            "type": info["type"],
            "description": d,
            "effect": effect_line(d) if d else "",
            "rebuild": bool(rebuild_scope(key)),
            "rebuild_scope": rebuild_scope(key),
            "screen_ids": screens_for(key, smap),
        }
        if "choices" in info:
            entry["choices"] = info["choices"]
        if "range" in info:
            entry["range"] = info["range"]
        for sid in sc:
            if sid in out_stages:
                out_stages[sid]["keys"].append(entry)

    # CLI flags
    man_cli: dict[str, str] = manual.get("cli_to_stage", {})
    for fl in cli_flags():
        sid = man_cli.get(fl["flag"])
        if sid is None:
            sid = next(
                (s for pat, s in CLI_STAGE_RULES if re.match(pat, fl["flag"])), HARNESS_STAGE
            )
        entry = {
            "key": f"cli:{fl['flag']}",
            "default": fl["default"],
            "type": fl["type"],
            "description": fl["help"],
            "effect": man_cli_effect(manual, fl["flag"])
            or (effect_line(fl["help"]) if fl["help"] else ""),
            "rebuild": False,
            "rebuild_scope": "ingest"
            if fl["flag"]
            in (
                "--memspine-config",
                "--memspine-batch-turns",
                "--memspine-build-sleep",
                "--memspine-llm",
            )
            else "",
            "screen_ids": screens_for(f"cli:{fl['flag']}", smap),
        }
        entry["rebuild"] = bool(entry["rebuild_scope"])
        if fl["choices"]:
            entry["choices"] = fl["choices"]
        out_stages[sid]["keys"].append(entry)

    # engine prompts -> stages
    pfiles = prompt_files()
    names = (
        {p["_stem"].split("@")[0] for p in pfiles}
        | {p["prompt_id"].split("@")[0] for p in pfiles}
        | {p["role"] for p in pfiles if p["role"]}
    )
    sites = string_sites(names)
    man_prompt: dict[str, list[str]] = manual.get("prompt_to_stages", {})
    role_models: dict[str, set[str]] = defaultdict(set)
    extra = [
        {**e, "_stem": e["prompt_id"], "_extra": True} for e in manual.get("extra_prompts", [])
    ]
    for p in pfiles + extra:
        pid = p["prompt_id"]
        base = pid.split("@")[0]
        sids: dict[str, int] = defaultdict(int)
        curated = False
        for pat, ss in man_prompt.items():
            if fnmatch.fnmatchcase(pid, pat):
                curated = True
                for s in ss:
                    sids[s] += 100
                break
        if not curated:  # scan fallback: only call sites inside a function the inventory names
            for nm in {base, p["_stem"].split("@")[0], p["role"]}:
                for h in sites.get(nm, ()):
                    sc = site_stage_scores(h, stages, defs_in)
                    if sc and max(sc.values()) >= 10:
                        best = max(sc.values())
                        for s, v in sc.items():
                            if v == best:
                                sids[s] += 1
        ranked = [s for s, _ in sorted(sids.items(), key=lambda kv: (-kv[1], kv[0]))[:4]]
        rec = {k: v for k, v in p.items() if not k.startswith("_")}
        for s in ranked:
            if rec not in out_stages[s]["prompts"]:
                out_stages[s]["prompts"].append(rec)
            if p["role"]:
                role_models[s].add(p["role"])
    hp = harness_prompts()
    for sid, lst in hp.items():
        out_stages[sid]["prompts"].extend(lst)
        out_stages[sid]["models"].append(
            {
                "role": "reader" if sid == "C32" else "judge",
                "source": "cli:--reader-model" if sid == "C32" else "cli:--judge-model",
            }
        )

    # models: engine LLM roles named by the stage's prompts + model-valued keys
    for sid, st in out_stages.items():
        have = {m["role"] for m in st["models"]}
        for r in sorted(role_models.get(sid, ())):
            if r not in have:
                st["models"].append({"role": r, "source": f"llm.roles.{r}.model"})
        for k in st["keys"]:
            if k["key"].startswith("cli:"):
                continue
            if re.search(
                r"(^|\.)(\w*model|\w*_model_\w+|embedding\.provider)$", k["key"]
            ) and not k["key"].startswith("model.placement.catalogue"):
                m = {"role": k["key"], "source": k["key"], "default": k["default"]}
                if m not in st["models"]:
                    st["models"].append(m)
        st["keys"].sort(key=lambda e: e["key"])
        st["prompts"].sort(key=lambda e: e["prompt_id"])
        st["models"].sort(key=lambda e: e["role"])

    return {
        "_doc": "TI-8 stage -> parameter catalogue. Generated by scripts/build_stage_params.py; do not hand-edit (use docs/v2/data/stage_params_manual.json).",
        "stages": out_stages,
        "unattributed": unattributed,
        "unattributed_count": len(unattributed),
        "schema_key_count": len(keys),
    }


def man_cli_effect(manual: dict, flag: str) -> str:
    return manual.get("cli_effect", {}).get(flag, "")


# --------------------------------------------------------------------------- markdown


def _cell(v) -> str:
    s = json.dumps(v) if not isinstance(v, str) else v
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= 60 else s[:57] + "..."


def render_md(data: dict) -> str:
    L = [
        "# Stage parameter catalogue",
        "",
        "Generated by `scripts/build_stage_params.py` from the config schema, the engine source and the harness CLI; "
        "machine form in `docs/v2/data/stage_params.json`. Do not edit by hand: curate `docs/v2/data/stage_params_manual.json`.",
        "",
        "A key is listed under every stage whose code reads it (AST scan of attribute reads, matched to the file:function of "
        "[PIPELINE_STAGE_INVENTORY.md](PIPELINE_STAGE_INVENTORY.md)). `rebuild` says what a change forces when data already exists: "
        "`ingest` (re-ingest), `index` (rebuild the vector, lexical or graph index) or `sleep` (re-run the sleep stages); "
        "empty means a read-time switch. `effect` is the first sentence of the key's `docs/USAGE.md` description (the `--help` text for CLI flags). "
        "Screens are the S-numbers of `evals/v2_queue.tsv` and [SCREENS.md](SCREENS.md) that varied the key (directly or through a profile preset).",
        "",
        f"Schema keys: {data['schema_key_count']}; not attributed to any stage: {data['unattributed_count']} (listed at the end).",
        "",
    ]
    for sid, st in data["stages"].items():
        if not (st["keys"] or st["prompts"] or st["models"]):
            L += [f"## {sid} {st['name']}", "", st["file_function"], "", "No tunable keys.", ""]
            continue
        L += [f"## {sid} {st['name']}", "", st["file_function"], ""]
        if st["keys"]:
            L += [
                "| Key | Default | Type / range | Effect | Rebuild | Screens |",
                "|---|---|---|---|---|---|",
            ]
            for k in st["keys"]:
                if "choices" in k:
                    tr = " \\| ".join(str(c) for c in k["choices"])
                else:
                    tr = k["type"]
                    if "range" in k:
                        tr += " " + ", ".join(f"{a} {b}" for a, b in k["range"].items())
                eff = (k["effect"] or "").replace("|", "\\|")[:200]
                L.append(
                    f"| `{k['key']}` | `{_cell(k['default'])}` | {_cell(tr)} | {eff} | "
                    f"{k['rebuild_scope'] or '-'} | {', '.join(k['screen_ids']) or '-'} |"
                )
            L.append("")
        if st["prompts"]:
            L += ["Prompts:", ""]
            for p in st["prompts"]:
                L.append(
                    f"- `{p['prompt_id']}` ({p['role'] or '-'}), v{p['version'] or '-'}, `{p['file']}`"
                )
            L.append("")
        if st["models"]:
            L += ["Models:", ""]
            for m in st["models"]:
                dflt = f", default `{_cell(m['default'])}`" if "default" in m else ""
                L.append(f"- {m['role']} (`{m['source']}`{dflt})")
            L.append("")
    L += ["## Not attributed to a stage", ""]
    L += ["| Key | Reason |", "|---|---|"]
    for k, r in data["unattributed"].items():
        L.append(f"| `{k}` | {r} |")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    data = build()
    js = json.dumps(data, indent=1, sort_keys=False, ensure_ascii=False) + "\n"
    md = render_md(data)
    if args.check:
        cur_js = OUT_JSON.read_text(encoding="utf-8") if OUT_JSON.exists() else ""
        cur_md = OUT_MD.read_text(encoding="utf-8") if OUT_MD.exists() else ""
        if cur_js != js or cur_md != md:
            print(
                "stage_params out of date: run python scripts/build_stage_params.py",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT_JSON.write_text(js, encoding="utf-8", newline="\n")
    OUT_MD.write_text(md, encoding="utf-8", newline="\n")
    n_keys = sum(len(s["keys"]) for s in data["stages"].values())
    print(
        f"stages={len(data['stages'])} key-slots={n_keys} unattributed={data['unattributed_count']}/{data['schema_key_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
