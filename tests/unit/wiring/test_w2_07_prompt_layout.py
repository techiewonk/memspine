"""W2-07 (W-18, P-07, P-08, P-10): ``prompts.layout_v2`` prompt layout. Model-free."""

from __future__ import annotations

import difflib
import re
from typing import Any

import pytest

from memspine.config.schema import MemspineConfig
from memspine.config.schema.llm import PromptsConfig
from memspine.observability import tap
from memspine.prompts import layout, lint
from memspine.prompts.env import partials_dir
from memspine.prompts.identity import PromptIdentity, hash_prefix
from memspine.prompts.layout_language import detect_language
from memspine.prompts.loader import load_v2_pack
from memspine.prompts.registry import PromptRegistry

V2_IDS = sorted(p.id for p in load_v2_pack())

EN = "[1] [2024-03-02] Ilse: Bram and I went hiking last Friday.\n[2] [2024-03-02] Bram: I hope to visit Noor next month."
DE = (
    "[1] [2024-03-02] Ilse: Bram und ich sind letzten Freitag wandern gewesen, und es war sehr schön.\n"
    "[2] [2024-03-02] Bram: Ich hoffe, dass ich Noor nächsten Monat besuchen kann."
)
FR = (
    "[1] [2024-03-02] Ilse: Bram et moi sommes allés faire une randonnée vendredi dernier, c'est très bien.\n"
    "[2] [2024-03-02] Bram: J'espère que je pourrai rendre visite à Noor le mois prochain."
)
ZH = "[1] [2024-03-02] Ilse: 我和Bram上周五去远足了。\n[2] [2024-03-02] Bram: 我希望下个月去看望Noor。"


def ctx(
    pid: str, content: str = EN, sections: tuple[str, ...] = ("facts", "edges", "episode")
) -> dict[str, Any]:
    out: dict[str, Any] = {"content": content}
    if pid == "window_extract":
        out |= {"sections": list(sections), "turn_count": 2, "participants": "Ilse, Bram"}
    return out


def _sys(msgs: list[dict[str, str]]) -> str:
    return next(m["content"] for m in msgs if m["role"] == "system")


def _usr(msgs: list[dict[str, str]]) -> str:
    return next(m["content"] for m in msgs if m["role"] == "user")


def test_flag_default_off() -> None:
    cfg = PromptsConfig()
    assert cfg.layout_v2 is False
    assert cfg.layout_partials == ["same_language", "coarse_date"]
    assert MemspineConfig().prompts.layout_v2 is False


def test_v2_pack_covers_the_write_prompts() -> None:
    assert V2_IDS == [
        "extract@session3",
        "extract@session4",
        "extract_edges",
        "extract_edges@session",
        "window_extract",
    ]


# ------------------------------------------------------------------ flag off: byte-identical


@pytest.mark.parametrize("pid", V2_IDS)
def test_flag_off_is_byte_identical_and_untagged(pid: str) -> None:
    default = PromptRegistry()
    explicit_off = PromptRegistry(layout_v2=False, layout_partials=["coarse_date"])
    for content in (EN, DE):
        a, b = (
            default.get(pid).render(ctx(pid, content)),
            explicit_off.get(pid).render(ctx(pid, content)),
        )
        assert list(a) == list(b)
        assert a.prompt_version == b.prompt_version
        assert a.identity is None and b.identity is None
    # the v1 text is untouched: it still carries what v2 removes
    assert "written in English" not in _usr(default.get(pid).render(ctx(pid, DE)))
    assert "same_language" not in default.get(pid).body


def test_flag_off_registry_ignores_the_v2_pack() -> None:
    reg = PromptRegistry()
    assert reg.get("window_extract").version == 1
    assert "Example." in reg.get("extract@session4").system


# ------------------------------------------------------------------ flag on: English


@pytest.mark.parametrize("pid", V2_IDS)
def test_no_few_shot_examples_remain_under_v2(pid: str) -> None:
    reg = PromptRegistry(layout_v2=True)
    p = reg.get(pid)
    for content in (EN, DE):
        msgs = p.render(ctx(pid, content))
        assert layout._example_violations(_sys(msgs), "system") == []
        assert layout._example_violations(_usr(msgs).replace(content, ""), "user") == []
        assert "Example." not in _sys(msgs)
    assert layout.check_prompt(p, require_language=False) == []


@pytest.mark.parametrize("pid", V2_IDS)
def test_english_differs_only_by_documented_moves(pid: str) -> None:
    """Partials off: the only differences are removed example text, section schemas moved
    from the system to the user part (window_extract) and the reply-keys sentence."""
    off = PromptRegistry().get(pid).render(ctx(pid))
    on = PromptRegistry(layout_v2=True, layout_partials=[]).get(pid).render(ctx(pid))
    assert on.identity is not None and on.identity.partials == ()
    flat = lambda s: " ".join(s.split())
    v1_all, v2_all = flat(_sys(off) + " " + _usr(off)), flat(_sys(on) + " " + _usr(on))
    removed = []
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(
        None, v1_all.split(), v2_all.split(), autojunk=False
    ).get_opcodes():
        if tag in ("delete", "replace"):
            removed.append(" ".join(v1_all.split()[i1:i2]))
    allowed = (
        "Example.",
        "e.g.",
        "went hiking",
        "activities",
        "Reply with ONE JSON",
        "keys are exactly",
        "until 2022",
    )
    if pid == "window_extract":
        # nothing of the section schemas may be lost: every SECTION line survives in the user part
        for name in ("facts", "edges", "episode"):
            assert f"SECTION {name}:" in _usr(on)
            assert f"SECTION {name}:" not in _sys(on)
        # the user message of v1 is a suffix of the v2 user message
        assert flat(_usr(off)).split("Window of")[1] in flat(_usr(on))
        assert _sys(on).count("untrusted DATA") == 1
    for chunk in removed:
        assert any(a in chunk for a in allowed) or pid == "window_extract", (pid, chunk)
    # English input never gets the same-language partial
    assert "written in English" not in _sys(on) + _usr(on)


@pytest.mark.parametrize("pid", V2_IDS)
def test_coarse_date_partial_is_in_the_stable_prefix(pid: str) -> None:
    on = PromptRegistry(layout_v2=True).get(pid).render(ctx(pid))
    assert "never invent a day or a month the lines do not give" in _sys(on)
    assert "coarse_date" in on.identity.partials
    off_cd = (
        PromptRegistry(layout_v2=True, layout_partials=["same_language"]).get(pid).render(ctx(pid))
    )
    assert "never invent a day" not in _sys(off_cd)
    assert "coarse_date" not in off_cd.identity.partials
    # removing the partial removes exactly its sentence
    assert (
        " ".join(_sys(on).split())
        .replace(" ".join((_partial("coarse_date.j2")).split()), "")
        .split()
        == " ".join(_sys(off_cd).split()).split()
    )


def _partial(name: str) -> str:
    from memspine.prompts.env import partials_dir

    return (partials_dir() / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ stable prefix / identity


@pytest.mark.parametrize("pid", V2_IDS)
def test_identity_hash_is_stable_across_inputs(pid: str) -> None:
    p = PromptRegistry(layout_v2=True).get(pid)
    hashes, systems = set(), set()
    for content in (EN, DE, FR, ZH, "x"):
        msgs = p.render(ctx(pid, content, sections=("facts",) if pid == "window_extract" else ()))
        hashes.add(msgs.identity.identity_hash)
        systems.add(_sys(msgs))
    assert len(hashes) == 1 and len(systems) == 1
    # window_extract: also stable across the requested sections (split mode)
    if pid == "window_extract":
        a = p.render(ctx(pid, sections=("facts",))).identity.identity_hash
        b = p.render(ctx(pid, sections=("edges", "episode"))).identity.identity_hash
        assert a == b == next(iter(hashes))


def test_v1_window_extract_prefix_was_not_stable() -> None:
    p = PromptRegistry().get("window_extract")
    a, b = p.render(ctx("window_extract", sections=("facts",))), p.render(ctx("window_extract"))
    assert _sys(a) != _sys(b)


def test_identity_value_object_and_hash_function() -> None:
    p = PromptRegistry(layout_v2=True).get("window_extract")
    msgs = p.render(ctx("window_extract"))
    ident = msgs.identity
    assert isinstance(ident, PromptIdentity)
    assert ident.identity_hash == hash_prefix(_sys(msgs))
    assert ident.prompt_id == "window_extract" and ident.prompt_version == p.prompt_version
    assert ident.short.startswith(p.prompt_version + "#")
    # the same hash as layout.build uses for a prefix
    lay = layout.PromptLayout(prefix=(layout.Section(name="x", text="plain prefix"),))
    assert layout.build(lay).prefix_hash == hash_prefix("plain prefix")


def test_layout_checker_accepts_the_v2_prompts_prefix() -> None:
    reg = PromptRegistry(layout_v2=True)
    for pid in V2_IDS:
        vs = layout.check_prompt(reg.get(pid), require_language=False)
        assert not [
            v for v in vs if v.rule in ("prefix_unstable", "variable_in_prefix", "example_block")
        ], (
            pid,
            vs,
        )


def test_gold_lint_covers_partials_and_v2_prompts() -> None:
    reg = PromptRegistry(layout_v2=True)
    texts = lint.render_prompt_texts(reg.list())
    assert len(texts) == len(reg.list())
    names = {p.name for p in lint.partial_files(partials_dir())}
    assert {"same_language.j2", "coarse_date.j2"} <= names
    for token in ("Ilse", "Bram", "Noor", "Tomasz"):
        assert (
            lint.lint_text(_partial("same_language.j2") + _partial("coarse_date.j2"), names=[token])
            == []
        )


# ------------------------------------------------------------------ same-language partial


@pytest.mark.parametrize(("text", "code"), [(EN, "en"), (DE, "de"), (FR, "fr"), (ZH, "zh")])
def test_language_detection(text: str, code: str) -> None:
    assert detect_language(text) == code


def test_language_detection_defaults_to_english() -> None:
    for t in ("", "ok", "Ilse", "12 34", "[1] [2024-03-02] Noor: Tomasz"):
        assert detect_language(t) == "en"
    assert detect_language("こんにちは、元気ですか。今日は天気がいいですね。") == "ja"


@pytest.mark.parametrize("pid", V2_IDS)
@pytest.mark.parametrize(("content", "code"), [(DE, "de"), (FR, "fr"), (ZH, "zh")])
def test_same_language_partial_injected_for_non_english(pid: str, content: str, code: str) -> None:
    msgs = PromptRegistry(layout_v2=True).get(pid).render(ctx(pid, content))
    rule = " ".join(_partial("same_language.j2").split())
    assert rule in " ".join(_usr(msgs).split())
    assert rule not in " ".join(_sys(msgs).split())  # the prefix stays input-independent
    assert msgs.identity.language == code
    assert "same_language" in msgs.identity.partials


@pytest.mark.parametrize("pid", V2_IDS)
def test_same_language_partial_absent_for_english_and_when_switched_off(pid: str) -> None:
    on = PromptRegistry(layout_v2=True).get(pid).render(ctx(pid, EN))
    assert "written in English" not in _usr(on)
    assert "same_language" not in on.identity.partials
    off = (
        PromptRegistry(layout_v2=True, layout_partials=["coarse_date"])
        .get(pid)
        .render(ctx(pid, DE))
    )
    assert "written in English" not in _usr(off)
    assert off.identity.partials == ("coarse_date",)


def test_explicit_language_in_context_wins() -> None:
    p = PromptRegistry(layout_v2=True).get("extract@session4")
    msgs = p.render({"content": EN, "language": "de"})
    assert "written in English" in _usr(msgs) and msgs.identity.language == "de"


def test_partial_text_matches_the_layout_constant() -> None:
    assert " ".join(_partial("same_language.j2").split()) == layout.SAME_LANGUAGE_RULE


def test_partial_edit_shifts_the_prompt_version() -> None:
    a = PromptRegistry(layout_v2=True).get("window_extract").prompt_version
    b = PromptRegistry(layout_v2=True, partials={"coarse_date.j2": "Other."}).get("window_extract")
    assert a != b.prompt_version


def test_user_override_of_a_v2_prompt_stays_v2() -> None:
    reg = PromptRegistry(layout_v2=True, overrides={"extract_edges": {"token_budget": 512}})
    msgs = reg.get("extract_edges").render(ctx("extract_edges", DE))
    assert msgs.identity is not None and "same_language" in msgs.identity.partials


# ------------------------------------------------------------------ tap


def test_prompt_render_tap_event() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    orig = tap.event
    try:
        tap.event = lambda stage, /, **f: seen.append((stage, f))  # type: ignore[assignment]
        from memspine.prompts import base

        base.tap.event = tap.event  # type: ignore[assignment]
        PromptRegistry().get("window_extract").render(ctx("window_extract"))
        assert seen == []  # flag off: no event
        msgs = (
            PromptRegistry(layout_v2=True).get("window_extract").render(ctx("window_extract", DE))
        )
    finally:
        tap.event = orig  # type: ignore[assignment]
    assert len(seen) == 1
    stage, f = seen[0]
    assert stage == "prompt.render"
    assert set(f) == {"prompt_id", "identity_hash", "language", "partials"}
    assert f["identity_hash"] == msgs.identity.identity_hash and f["language"] == "de"
    assert f["partials"] == ["same_language", "coarse_date"]


def test_config_flows_into_the_container_registry() -> None:
    from memspine.runtime.container import build_prompts

    cfg = MemspineConfig(prompts={"layout_v2": True, "layout_partials": ["coarse_date"]})
    reg = build_prompts(cfg)
    assert reg.get("window_extract").version == 2
    assert re.search(r"coarse_date", ",".join(reg.get("window_extract")._layout_partials))
    assert reg.get("window_extract")._layout_partials == ("coarse_date",)
