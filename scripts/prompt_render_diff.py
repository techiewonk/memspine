"""W2-07 render-diff report: ``prompts.layout`` off vs on, per prompt and per language.

Model-free. For each prompt the layout-v2 pack replaces, renders the v1 prompt and the v2
prompt over the canonical English context and prints a unified diff of the system and user
text with the token delta (whitespace words), then shows what changes for de/fr/zh input.
Usage: ``python scripts/prompt_render_diff.py [--full]``.
"""

from __future__ import annotations

import difflib
import sys

from memspine.prompts.loader import load_pack
from memspine.prompts.registry import PromptRegistry

EN = "[1] [2024-03-02] Ilse: Bram and I went hiking last Friday.\n[2] [2024-03-02] Bram: I hope to visit Noor next month."
SAMPLES = {
    "en": EN,
    "de": "[1] [2024-03-02] Ilse: Bram und ich sind letzten Freitag wandern gewesen, und es war sehr schön.\n"
    "[2] [2024-03-02] Bram: Ich hoffe, dass ich Noor nächsten Monat besuchen kann.",
    "fr": "[1] [2024-03-02] Ilse: Bram et moi sommes allés faire une randonnée vendredi dernier, c'est très bien.\n"
    "[2] [2024-03-02] Bram: J'espère que je pourrai rendre visite à Noor le mois prochain.",
    "zh": "[1] [2024-03-02] Ilse: 我和Bram上周五去远足了。\n[2] [2024-03-02] Bram: 我希望下个月去看望Noor。",
}


def ctx(pid: str, content: str) -> dict[str, object]:
    base: dict[str, object] = {"content": content}
    if pid == "window_extract":
        base |= {
            "sections": ["facts", "edges", "episode"],
            "turn_count": 2,
            "participants": "Ilse, Bram",
        }
    return base


def words(msgs: list[dict[str, str]]) -> int:
    return sum(len(m["content"].split()) for m in msgs)


def main(full: bool) -> None:
    off, on = PromptRegistry(), PromptRegistry(layout=True)
    for new in load_pack():
        pid = new.id
        a, b = off.get(pid).render(ctx(pid, EN)), on.get(pid).render(ctx(pid, EN))
        print(
            f"=== {pid}: v{off.get(pid).version} -> v{on.get(pid).version}; words {words(a)} -> {words(b)}"
        )
        print(
            f"    identity {b.identity.short if b.identity else None}; partials {b.identity.partials if b.identity else ()}"
        )
        for ma, mb in zip(a, b, strict=False):
            diff = list(
                difflib.unified_diff(
                    ma["content"].splitlines(),
                    mb["content"].splitlines(),
                    "v1",
                    "v2",
                    lineterm="",
                    n=0,
                )
            )
            print(
                f"  [{ma['role']}] changed lines: {sum(1 for d in diff if d[:1] in '+-' and d[:3] not in ('+++', '---'))}"
            )
            if full:
                print("\n".join("    " + d for d in diff))
        for lang in ("de", "fr", "zh"):
            m = on.get(pid).render(ctx(pid, SAMPLES[lang]))
            print(
                f"  {lang}: detected {m.identity.language} partials {m.identity.partials} same prefix hash: {m.identity.identity_hash == b.identity.identity_hash}"
            )


if __name__ == "__main__":
    main("--full" in sys.argv)
