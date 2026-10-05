"""Review-3 fixer E: crash-safe spend (C-2), credentials (C-3), chunked resume (C-4),
rerank audit (C-5), service prices (C-6), error rows (C-7), plan/rehearsal drift (C-8).

Everything runs offline: fakes or the stub LiteLLM transport, no request leaves.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.bedrock import CallBudget, LiteLLMReader, parse_service_price
from memspine_evals.contracts import DepositResult, ReaderAnswer, Turn
from memspine_evals.credentials import CredentialsInvalid, is_auth_error, preflight_aws
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.experiments import C01Config, check_dollar_cap
from memspine_evals.judge import ExactMatchJudge
from memspine_evals.provenance import RunProtocol
from memspine_evals.results import read_run
from memspine_evals.runner import EvalRunner, ProviderCredentialError, RunConfig
from memspine_evals.systems import VerbatimSystem

PROTOCOL = RunProtocol(protocol_id="fixE", budget_tokens=400, top_k=5, seed=11)
EXPIRED = "The security token included in the request is expired"


class PricedReader:
    """A reader that 'calls' a priced model: 1,000 prompt + 10 completion tokens."""

    reader_id = "priced"
    model = "priced-model"
    makes_model_calls = True

    def __init__(self, fail_on: int | None = None, message: str = EXPIRED) -> None:
        self.calls = 0
        self.fail_on = fail_on
        self.message = message

    def describe(self) -> Mapping[str, Any]:
        return {"reader_id": self.reader_id, "model": self.model}

    async def answer(self, question: str, context: str) -> ReaderAnswer:
        self.calls += 1
        if self.fail_on is not None and self.calls >= self.fail_on:
            raise RuntimeError(self.message)
        return ReaderAnswer(
            text="x", prompt_tokens=1000, completion_tokens=10, latency_ms=0.0, model_calls=1
        )


class InsertFailsOnItem(VerbatimSystem):
    """Raises during insert once the ``n``-th item starts."""

    def __init__(self, n: int, error: Exception) -> None:
        super().__init__()
        self._n, self._error, self._items = n, error, 0

    async def reset(self, item_id: str) -> None:
        self._items += 1
        await super().reset(item_id)

    async def insert(self, turn: Turn) -> DepositResult:
        if self._items >= self._n:
            raise self._error
        return await super().insert(turn)


def _runner(system: Any, reader: Any, tmp_path: Path, run_id: str) -> EvalRunner:
    dataset = SyntheticDataset(n_items=3, turns_per_item=8, facts_per_item=2)
    config = RunConfig(
        run_id=run_id,
        protocol=PROTOCOL,
        out_dir=tmp_path,
        prices_per_mtok={"priced-model": (1.0, 1.0)},
    )
    return EvalRunner(dataset, system, reader, ExactMatchJudge(), config)


def _summary(tmp_path: Path, run_id: str) -> dict[str, Any]:
    return json.loads((tmp_path / run_id / "summary.json").read_text(encoding="utf-8"))


# -- C-2: the spend record survives a crash -----------------------------------------


def test_crash_during_insert_still_writes_spend(tmp_path: Path) -> None:
    runner = _runner(
        InsertFailsOnItem(2, RuntimeError("disk full")), PricedReader(), tmp_path, "c2"
    )
    with pytest.raises(RuntimeError, match="disk full"):
        asyncio.run(runner.run())
    summary = _summary(tmp_path, "c2")
    assert summary["aborted"] is True and summary["abort_reason"] == "RuntimeError"
    assert summary["spend"]["usd"] > 0
    _, rows, _ = read_run(tmp_path / "c2" / "results.jsonl")
    assert rows and all(r["meta"]["spend_usd"] > 0 for r in rows)  # per-row spend (item 1)
    assert rows[-1]["meta"]["spend_usd_run"] == pytest.approx(summary["spend"]["usd"])


def test_clean_run_summary_is_not_aborted(tmp_path: Path) -> None:
    runner = _runner(VerbatimSystem(), PricedReader(), tmp_path, "clean")
    asyncio.run(runner.run())
    summary = _summary(tmp_path, "clean")
    assert summary["aborted"] is False and summary["abort_reason"] is None


def test_plan_report_and_run_chunked_read_aborted_summaries(tmp_path: Path) -> None:
    import plan_report
    import run_chunked

    runner = _runner(
        InsertFailsOnItem(2, RuntimeError("boom")),
        PricedReader(),
        tmp_path,
        "P--arm--verbatim-bm25",
    )
    with pytest.raises(RuntimeError):
        asyncio.run(runner.run())
    usd = _summary(tmp_path, "P--arm--verbatim-bm25")["spend"]["usd"]
    assert run_chunked.spent_usd(tmp_path, "P--arm", "verbatim-bm25") == pytest.approx(usd)
    assert run_chunked.spent_calls(tmp_path, "P--arm", "verbatim-bm25") >= 1
    table = plan_report.collect(tmp_path, "P")
    entry = table[("arm", "verbatim-bm25", "")]
    assert entry["usd"] == pytest.approx(usd) and entry["aborted"] is True
    assert entry["complete"] is False


# -- C-3: credentials ---------------------------------------------------------------


def test_expired_token_in_the_reader_stops_the_run_unattempted(tmp_path: Path) -> None:
    runner = _runner(VerbatimSystem(), PricedReader(fail_on=2), tmp_path, "c3")
    with pytest.raises(ProviderCredentialError):
        asyncio.run(runner.run())
    _, rows, _ = read_run(tmp_path / "c3" / "results.jsonl")
    statuses = [r["status"] for r in rows]
    assert "error" not in statuses
    assert statuses[0] == "completed" and statuses.count("unattempted") == len(rows) - 1
    assert len(rows) == 6  # 3 items x 2 questions: every scheduled question is accounted for
    summary = _summary(tmp_path, "c3")
    assert summary["aborted"] is True and summary["abort_reason"] == "ProviderCredentialError"


def test_expired_token_at_insert_stops_the_run_unattempted(tmp_path: Path) -> None:
    system = InsertFailsOnItem(2, RuntimeError(f"APIConnectionError: {EXPIRED}"))
    runner = _runner(system, PricedReader(), tmp_path, "c3i")
    with pytest.raises(ProviderCredentialError):
        asyncio.run(runner.run())
    _, rows, _ = read_run(tmp_path / "c3i" / "results.jsonl")
    assert [r["status"] for r in rows] == ["completed"] * 2 + ["unattempted"] * 4


def test_ordinary_reader_errors_stay_error_rows(tmp_path: Path) -> None:
    runner = _runner(VerbatimSystem(), PricedReader(fail_on=2, message="bad json"), tmp_path, "e")
    asyncio.run(runner.run())
    _, rows, _ = read_run(tmp_path / "e" / "results.jsonl")
    assert [r["status"] for r in rows].count("error") == 5


def test_is_auth_error_reads_the_cause_chain() -> None:
    class AuthenticationError(Exception):
        pass

    assert is_auth_error(RuntimeError(EXPIRED))
    assert is_auth_error(AuthenticationError("nope"))
    try:
        try:
            raise RuntimeError("UnrecognizedClientException: The security token is invalid")
        except RuntimeError as inner:
            raise ValueError("embedding failed") from inner
    except ValueError as outer:
        assert is_auth_error(outer)
    assert not is_auth_error(RuntimeError("Server disconnected"))


class _FakeSTS:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def get_caller_identity(self) -> dict[str, str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/x"}


def test_preflight_warns_when_the_session_expires_before_the_run() -> None:
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    warnings: list[str] = []
    check = preflight_aws(
        sts_client=_FakeSTS(),
        expected_seconds=3 * 3600,
        expiry_lookup=lambda: now + timedelta(minutes=30),
        now=lambda: now,
        warn=warnings.append,
    )
    assert check.account == "123456789012" and check.remaining_s == pytest.approx(1800)
    assert warnings and "expire in 30 min" in warnings[0]
    quiet: list[str] = []
    preflight_aws(
        sts_client=_FakeSTS(),
        expected_seconds=600,
        expiry_lookup=lambda: now + timedelta(hours=2),
        now=lambda: now,
        warn=quiet.append,
    )
    assert quiet == []


def test_preflight_refuses_rejected_or_expired_credentials() -> None:
    with pytest.raises(CredentialsInvalid, match="expired or invalid"):
        preflight_aws(sts_client=_FakeSTS(RuntimeError(EXPIRED)), expiry_lookup=lambda: None)
    now = datetime(2026, 10, 5, tzinfo=UTC)
    with pytest.raises(CredentialsInvalid, match="expired at"):
        preflight_aws(
            sts_client=_FakeSTS(),
            expiry_lookup=lambda: now - timedelta(minutes=1),
            now=lambda: now,
        )


class _FlakyLiteLLM:
    def __init__(self, errors: list[Exception]) -> None:
        self.errors = errors
        self.calls = 0

    async def acompletion(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=1),
        )


class APIConnectionError(Exception):
    """Named like litellm's transient class (the retry matches by class name)."""


def test_reader_retries_transient_errors_but_not_auth() -> None:
    reader = LiteLLMReader(CallBudget(max_calls=10), model="m", retry_base_delay=0.0)
    flaky = _FlakyLiteLLM([APIConnectionError("Server disconnected"), APIConnectionError("again")])
    reader._litellm = flaky
    answer = asyncio.run(reader.answer("q", "c"))
    assert answer.text == "ok" and flaky.calls == 3
    assert reader.budget.calls == 1  # one reservation covers the retries
    expired = _FlakyLiteLLM([APIConnectionError(EXPIRED)])
    reader._litellm = expired
    with pytest.raises(APIConnectionError):
        asyncio.run(reader.answer("q", "c"))
    assert expired.calls == 1


# -- C-4: run_chunked matches exactly one arm ---------------------------------------


def _write(runs: Path, name: str, rows: list[tuple[str, str]], summary: Any = None) -> Path:
    d = runs / name
    d.mkdir(parents=True)
    lines = [json.dumps({"kind": "manifest"})]
    lines += [
        json.dumps(
            {
                "kind": "result",
                "item_id": item,
                "query_id": f"{item}-q",
                "status": status,
                "score": 1.0 if status == "completed" else None,
                "type_label": "cat1",
            }
        )
        for item, status in rows
    ]
    (d / "results.jsonl").write_text("\n".join(lines), encoding="utf-8")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return d


def test_other_arms_and_repeats_do_not_count_toward_an_arm(tmp_path: Path) -> None:
    import run_chunked

    _write(tmp_path, "P--combo-A-pool--memspine", [("a", "completed"), ("b", "completed")])
    _write(tmp_path, "P--combo-A--r1--memspine", [("c", "completed")])
    _write(tmp_path, "P--combo-A--memspine", [("d", "completed"), ("e", "error")])
    _write(tmp_path, "P--combo-A-chunk00--memspine", [("e", "completed")])
    _write(tmp_path, "P--combo-A-resume--memspine", [("f", "completed")])
    assert run_chunked.done_items(tmp_path, "P--combo-A", "memspine") == {"d", "e", "f"}
    assert run_chunked.done_items(tmp_path, "P--combo-A--r1", "memspine") == {"c"}
    assert run_chunked.next_chunk_number(tmp_path, "P--combo-A", "memspine") == 1


def test_chunks_are_read_as_parts_of_their_arm(tmp_path: Path) -> None:
    import plan_report

    m = plan_report._DIR.match("P--combo-A-chunk03--memspine")
    assert m and m["arm"] == "combo-A" and not m["rep"]
    m = plan_report._DIR.match("P--combo-A--r2-chunk00--memspine")
    assert m and m["arm"] == "combo-A" and m["rep"] == "2"
    _write(
        tmp_path, "P--combo-A--memspine", [("d", "completed"), ("e", "error")], {"aborted": True}
    )
    _write(tmp_path, "P--combo-A-chunk00--memspine", [("e", "completed")], {"aborted": False})
    table = plan_report.collect(tmp_path, "P")
    assert set(table) == {("combo-A", "memspine", "")}
    entry = table[("combo-A", "memspine", "")]
    assert entry["n"] == 2 and entry["errors"] == 0 and entry["complete"] is True


def test_max_usd_is_split_across_chunks(tmp_path: Path) -> None:
    import run_chunked

    rest, usd = run_chunked.split_usd(["--mode", "qa", "--max-usd", "5", "--out", "X"])
    assert usd == 5.0 and "--max-usd" not in rest
    _write(tmp_path, "job-chunk00--memspine", [("a", "completed")], {"spend": {"usd": 1.25}})
    assert run_chunked.spent_usd(tmp_path, "job-chunk00", "memspine") == pytest.approx(1.25)
    cmd = run_chunked.chunk_command(rest, "memspine", ["b"], "job-chunk01", tmp_path, 10, 3.75)
    assert cmd.count("--max-usd") == 1 and float(cmd[cmd.index("--max-usd") + 1]) == 3.75


# -- C-5: the rerank audit ----------------------------------------------------------


def test_rehearsing_r_cohere_stubs_the_rerank(tmp_path: Path) -> None:
    from memspine_evals.rehearsal import load_plan, rehearse

    plan = load_plan(Path(__file__).resolve().parents[1] / "plans" / "aamas_runs.json")
    dataset = SyntheticDataset(n_items=1, turns_per_item=12, facts_per_item=3)
    (report,) = asyncio.run(
        rehearse(
            plan, dataset, tmp_path, item_ids=None, max_queries=None, prices={},
            arm_ids=("R-cohere",),
        )
    )  # fmt: skip
    assert report.ok, report.problems
    assert report.stub_calls.get("rerank", 0) >= 1
    (measure,) = report.systems
    assert measure.rerank["mode"] == "litellm" and measure.rerank["calls"] >= 1
    assert measure.rerank["rerank_unavailable"] is False
    _, rows, _ = read_run(tmp_path / "rehearsal-R-cohere--memspine" / "results.jsonl")
    assert any(r["meta"].get("reranked") for r in rows)


class _RerankMetaSystem(VerbatimSystem):
    """Reports a configured reranker that never returned scores."""

    async def query(self, text: str, budget_tokens: int, top_k: int) -> Any:
        context = await super().query(text, budget_tokens, top_k)
        meta = {**context.meta, "rerank_mode": "litellm", "reranked": False,
                "rerank_calls": 1, "rerank_failures": 1}  # fmt: skip
        return type(context)(
            text=context.text, tokens=context.tokens, evidence=context.evidence,
            truncated=context.truncated, boundary_index=context.boundary_index, meta=meta,
        )  # fmt: skip


def test_a_reranker_that_never_ran_is_flagged(tmp_path: Path) -> None:
    runner = _runner(_RerankMetaSystem(), PricedReader(), tmp_path, "rr")
    asyncio.run(runner.run())
    summary = _summary(tmp_path, "rr")
    assert summary["rerank_unavailable"] is True
    assert summary["rerank"]["failures"] == summary["rerank"]["queries"] == 6


# -- C-6: paid embedder and reranker prices -----------------------------------------

RERANK = {"read": {"rerank": "litellm", "rerank_model": "cohere.rerank-v3-5:0"}}
QA = {"mode": "qa", "bedrock": True, "max_model_calls": 10, "max_usd": 1.0}


def test_dollar_cap_refuses_an_unpriced_reranker() -> None:
    with pytest.raises(ValueError, match=re.escape("rerank:cohere.rerank-v3-5:0")):
        check_dollar_cap(C01Config(include_memspine=True, memspine_config=RERANK, **QA))
    check_dollar_cap(
        C01Config(
            include_memspine=True,
            memspine_config=RERANK,
            service_prices=(("rerank", "cohere.rerank-v3-5:0", 2.0),),
            **QA,
        )
    )


def test_dollar_cap_refuses_an_unpriced_cloud_embedder() -> None:
    embed = {"embedding": {"provider": "litellm", "model": "bedrock/cohere.embed-v4:0"}}
    with pytest.raises(ValueError, match=re.escape("embed:bedrock/cohere.embed-v4:0")):
        check_dollar_cap(C01Config(include_memspine=True, memspine_config=embed, **QA))
    local = {"embedding": {"provider": "hash"}}
    check_dollar_cap(C01Config(include_memspine=True, memspine_config=local, **QA))


def test_service_prices_parse_and_charge() -> None:
    from memspine_evals.cli import parse_prices, parse_service_prices

    specs = ["m=1,2", "embed:bedrock/cohere.embed-v4:0=0.12", "rerank:arn:x/cohere:0=2"]
    assert parse_service_price("m=1,2") is None
    assert parse_prices(specs) == (("m", 1.0, 2.0),)
    assert parse_service_prices(specs) == (
        ("embed", "bedrock/cohere.embed-v4:0", 0.12),
        ("rerank", "arn:x/cohere:0", 2.0),
    )
    budget = CallBudget(
        max_calls=10,
        prices_per_mtok={},
        service_prices={"embed:e": 0.10, "rerank:r": 2.0},
        max_usd=0.004,
    )
    budget.charge_service("embed", "e", 10_000)  # $0.001
    budget.charge_service("rerank", "r", 1)  # $0.002
    assert budget.spent_usd() == pytest.approx(0.003)
    assert budget.summary()["services_usd"] == pytest.approx(0.003)
    budget.charge_service("rerank", "r", 1)
    from memspine_evals.bedrock import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        budget.check_usd()


# -- C-7: error rows ----------------------------------------------------------------


def test_plan_report_marks_arms_with_error_rows_partial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import plan_report

    _write(tmp_path, "P--x--memspine", [("a", "completed"), ("b", "error")], {"aborted": False})
    entry = plan_report.collect(tmp_path, "P")[("x", "memspine", "")]
    assert entry["errors"] == 1 and entry["complete"] is False and entry["acc"] == 50.0
    plan_report.main(["--runs", str(tmp_path), "--prefix", "P", "--n", "2"])
    assert "x (partial) [1 error rows, scored 0]" in capsys.readouterr().out


# -- C-8: rehearsal validates every policy block ------------------------------------


def test_validate_engine_config_checks_every_policy_block() -> None:
    from memspine_evals.rehearsal import validate_engine_config

    good = {"memories": {"semantic": {"enabled": True, "policies": {"conflict": {}}}}}
    validate_engine_config(good)
    bad = {"memories": {"semantic": {"enabled": True, "policies": {"conflict": {"nope": 1}}}}}
    with pytest.raises(ValueError):
        validate_engine_config(bad)
    unknown = {"memories": {"semantic": {"enabled": True, "policies": {"conflcit": {}}}}}
    with pytest.raises(ValueError, match="unknown policy"):
        validate_engine_config(unknown)
    ns_bad = {"namespaces": {"eval": {"policies": {"dedup": {"nope": 1}}}}}
    with pytest.raises(ValueError):
        validate_engine_config(ns_bad)
