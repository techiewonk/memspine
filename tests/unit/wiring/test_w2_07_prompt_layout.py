"""W2-07 (W-18, P-07, P-08, P-10): ``prompts.layout`` prompt layout. Model-free."""

from __future__ import annotations

import re
from typing import Any

import pytest

from memspine.config.schema import MemspineConfig
from memspine.observability import tap
from memspine.prompts import layout, lint
from memspine.prompts.env import partials_dir
from memspine.prompts.identity import PromptIdentity, hash_prefix
from memspine.prompts.layout_language import detect_language
from memspine.prompts.registry import PromptRegistry

BASE_IDS = ["extract@session", "extract_edges", "extract_edges@session", "window_extract"]

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


# ------------------------------------------------------------------ flag off: byte-identical


# ------------------------------------------------------------------ flag on: English


@pytest.mark.parametrize("pid", BASE_IDS)
def test_no_few_shot_examples_remain_in_base(pid: str) -> None:
    reg = PromptRegistry()
    p = reg.get(pid)
    for content in (EN, DE):
        msgs = p.render(ctx(pid, content))
        assert layout._example_violations(_sys(msgs), "system") == []
        assert layout._example_violations(_usr(msgs).replace(content, ""), "user") == []
        assert "Example." not in _sys(msgs)
    assert layout.check_prompt(p, require_language=False) == []


@pytest.mark.parametrize("pid", BASE_IDS)
def test_coarse_date_partial_is_in_the_stable_prefix(pid: str) -> None:
    on = PromptRegistry().get(pid).render(ctx(pid))
    assert "never invent a day or a month the lines do not give" in _sys(on)
    assert "coarse_date" in on.identity.partials
    off_cd = PromptRegistry(layout_partials=["same_language"]).get(pid).render(ctx(pid))
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


@pytest.mark.parametrize("pid", BASE_IDS)
def test_identity_hash_is_stable_across_inputs(pid: str) -> None:
    p = PromptRegistry().get(pid)
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


def test_identity_value_object_and_hash_function() -> None:
    p = PromptRegistry().get("window_extract")
    msgs = p.render(ctx("window_extract"))
    ident = msgs.identity
    assert isinstance(ident, PromptIdentity)
    assert ident.identity_hash == hash_prefix(_sys(msgs))
    assert ident.prompt_id == "window_extract" and ident.prompt_version == p.prompt_version
    assert ident.short.startswith(p.prompt_version + "#")
    # the same hash as layout.build uses for a prefix
    lay = layout.PromptLayout(prefix=(layout.Section(name="x", text="plain prefix"),))
    assert layout.build(lay).prefix_hash == hash_prefix("plain prefix")


def test_layout_checker_accepts_the_base_prompts_prefix() -> None:
    reg = PromptRegistry()
    for pid in BASE_IDS:
        vs = layout.check_prompt(reg.get(pid), require_language=False)
        assert not [
            v for v in vs if v.rule in ("prefix_unstable", "variable_in_prefix", "example_block")
        ], (
            pid,
            vs,
        )


def test_gold_lint_covers_partials_and_base_prompts() -> None:
    reg = PromptRegistry()
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


@pytest.mark.parametrize("pid", BASE_IDS)
@pytest.mark.parametrize(("content", "code"), [(DE, "de"), (FR, "fr"), (ZH, "zh")])
def test_same_language_partial_injected_for_non_english(pid: str, content: str, code: str) -> None:
    msgs = PromptRegistry().get(pid).render(ctx(pid, content))
    rule = " ".join(_partial("same_language.j2").split())
    assert rule in " ".join(_usr(msgs).split())
    assert rule not in " ".join(_sys(msgs).split())  # the prefix stays input-independent
    assert msgs.identity.language == code
    assert "same_language" in msgs.identity.partials


@pytest.mark.parametrize("pid", BASE_IDS)
def test_same_language_partial_absent_for_english_and_when_switched_off(pid: str) -> None:
    on = PromptRegistry().get(pid).render(ctx(pid, EN))
    assert "written in English" not in _usr(on)
    assert "same_language" not in on.identity.partials
    off = PromptRegistry(layout_partials=["coarse_date"]).get(pid).render(ctx(pid, DE))
    assert "written in English" not in _usr(off)
    assert off.identity.partials == ("coarse_date",)


def test_explicit_language_in_context_wins() -> None:
    p = PromptRegistry().get("extract@session")
    msgs = p.render({"content": EN, "language": "de"})
    assert "written in English" in _usr(msgs) and msgs.identity.language == "de"


def test_partial_text_matches_the_layout_constant() -> None:
    assert " ".join(_partial("same_language.j2").split()) == layout.SAME_LANGUAGE_RULE


def test_partial_edit_shifts_the_prompt_version() -> None:
    a = PromptRegistry().get("window_extract").prompt_version
    b = PromptRegistry(partials={"coarse_date.j2": "Other."}).get("window_extract")
    assert a != b.prompt_version


def test_user_override_of_a_v2_prompt_stays_v2() -> None:
    reg = PromptRegistry(overrides={"extract_edges": {"token_budget": 512}})
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
        assert len(seen) == 1
        seen.clear()
        msgs = PromptRegistry().get("window_extract").render(ctx("window_extract", DE))
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

    cfg = MemspineConfig(
        prompts={"layout_partials": ["coarse_date"]},
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        },
    )
    reg = build_prompts(cfg)
    assert reg.get("window_extract").version == 2
    assert re.search(r"coarse_date", ",".join(reg.get("window_extract")._layout_partials))
    assert reg.get("window_extract")._layout_partials == ("coarse_date",)
