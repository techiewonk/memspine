"""Static content/link/JavaScript checks for the documentation-only review."""

import hashlib
import html
import json
import re
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

P = Path(__file__).resolve().parent / "independent_review_2026-10-11"
page = (P / "index.html").read_text(encoding="utf-8")
visible = html.unescape(page)


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])


p = Page()
p.feed(page)
assert len(p.ids) == len(set(p.ids)), "duplicate anchor"
for link in p.links:
    if link.startswith("#"):
        assert link[1:] in p.ids, link
    elif not link.startswith(("https://", "http://")):
        assert (P / link.split("#")[0]).exists(), link


def strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)
    elif isinstance(obj, str):
        yield obj


checked = 0
for f in (P / "sources").glob("*.json"):
    if f.name == "all_scope_coverage.json":
        continue
    obj = json.loads(f.read_text(encoding="utf-8"))
    # All input records, gap cards, prompt cells, catalogue and Claude reports
    # are rendered; synthesis is preserved as a separate historical source.
    for text in strings(obj):
        assert text in visible, (f.name, text[:100])
        checked += 1

scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
for script in scripts:
    with tempfile.TemporaryDirectory(prefix="memspine-review-js-") as d:
        f = Path(d) / "review.js"
        f.write_text(script, encoding="utf-8")
        subprocess.run(["node", "--check", str(f)], check=True, capture_output=True)
v = json.loads((P / "validation.json").read_text())
codex_path = P / "codex_verification.json"
if codex_path.exists():
    codex = json.loads(codex_path.read_text(encoding="utf-8"))
    for text in strings(codex):
        assert text in visible, ("codex_verification.json", text[:100])
    assert len(codex["engine_checks"]) == 16
    assert len(codex["gap_checks"]) == 41
    assert len(codex["inherited_cell_ledger"]) == 1200
    assert len({r["id"] for r in codex["gap_checks"]}) == 41
    from build_codex_verification import CLONES, ROOT, evidence

    refs = 0
    for engine in codex["engine_checks"]:
        base = ROOT if engine["slug"] == "memspine" else CLONES / engine["clone"]
        for item in engine["checks"]:
            e = item["evidence"]
            assert evidence(base, e["path"], e["line_start"], e["line_end"]) == e
            refs += 1
    for gap in codex["gap_checks"]:
        e = gap["evidence"]
        assert evidence(ROOT, e["path"], e["line_start"], e["line_end"]) == e
        refs += 1
    v["codex_source_references_hash_checked"] = refs
    v["inherited_cells_explicitly_not_certified"] = 1200
v.update(
    rendered_input_strings_checked=checked,
    local_links_checked=sum(not x.startswith(("https://", "http://")) for x in p.links),
    javascript_syntax_checked=len(scripts),
    html_sha256=hashlib.sha256(page.encode()).hexdigest(),
    browser_render_check=False,
)
(P / "validation.json").write_text(json.dumps(v, indent=2), encoding="utf-8")
print(json.dumps(v, indent=2))
