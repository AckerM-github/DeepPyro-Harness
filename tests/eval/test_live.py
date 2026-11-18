"""Live eval runner tests: scripted providers only, so nothing opens a socket."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.errors import ProviderStreamError
from deeppyro.config import Config, ModelCost, ModelProfile
from deeppyro.core.llm import Finish, ModelRequest, StopReason, StreamEvent, TextDelta, Usage, UsageEvent
from deeppyro.eval import (
    EvalBudget,
    EvalCase,
    EvalExpectation,
    EvalOutcome,
    EvalReport,
    EvalValidationError,
)
from deeppyro.eval.live import (
    LIVE_ENV_VAR,
    LiveAgentRunner,
    LiveCaseMetrics,
    LiveEvalReport,
    main,
    resolve_suite,
)

#: Environment variable the "no API key" diagnostic points at; kept unset below.
TEST_API_KEY_ENV = "DEEPPYRO_LIVE_TEST_KEY"


@pytest.fixture(autouse=True)
def _isolated_live_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep sessions, traces and the config home inside the test's tmp directory."""
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEEPPYRO_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("DEEPPYRO_TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.delenv(LIVE_ENV_VAR, raising=False)
    monkeypatch.delenv(TEST_API_KEY_ENV, raising=False)


class _ScriptedProvider:
    """Deterministic offline LlmProvider: answers are scripted stream events."""

    name = "scripted"

    def __init__(self, script: Sequence[Any]) -> None:
        self.script = list(script)
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def stream(self, request: ModelRequest) -> Any:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        for event in item:
            yield event

    async def aclose(self) -> None:
        self.closed = True


def _simple_response(text: str, *, usage: Usage | None = None) -> tuple[StreamEvent, ...]:
    return (
        TextDelta(text=text),
        UsageEvent(usage if usage is not None else Usage(input_tokens=3, output_tokens=2, total_tokens=5)),
        Finish(stop_reason=StopReason.STOP),
    )


def _profile() -> ModelProfile:
    return ModelProfile(
        id="test-model",
        provider="fake",
        cost=ModelCost(input_per_mtok=1.0, output_per_mtok=2.0),
    )


def _runner(
    tmp_path: Path,
    script: Sequence[Any],
    *,
    profile: ModelProfile | None = None,
) -> tuple[LiveAgentRunner, _ScriptedProvider]:
    provider = _ScriptedProvider(script)
    runner = LiveAgentRunner(
        provider=provider,
        model="test-model",
        workdir=tmp_path / "work",
        traces_root=tmp_path / "traces",
        sessions_root=tmp_path / "sessions",
        provider_name="fake",
        profile=profile,
    )
    return runner, provider


def _config(*, api_key: str | None = "test-key") -> Config:
    provider: dict[str, Any] = {
        "api": "openai",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key_env": TEST_API_KEY_ENV,
        "models": [
            {
                "id": "test-model",
                "cost": {"input_per_mtok": 1.0, "output_per_mtok": 2.0},
            }
        ],
    }
    if api_key is not None:
        provider["api_key"] = api_key
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "test-model"},
            "providers": {"local": provider},
        }
    )


async def test_runner_scores_cases_and_records_metrics(tmp_path: Path) -> None:
    runner, provider = _runner(
        tmp_path,
        [_simple_response("alpha answer"), _simple_response("beta answer")],
        profile=_profile(),
    )
    cases = (
        EvalCase(
            id="live-alpha",
            prompt="say alpha",
            expect=EvalExpectation(final_text_contains=("alpha",)),
        ),
        EvalCase(
            id="live-beta",
            prompt="say beta",
            expect=EvalExpectation(final_text_contains=("beta",)),
        ),
    )
    report = await runner.run_cases(cases, suite="unit")
    assert isinstance(report, LiveEvalReport)
    assert report.total == 2
    assert report.passed_count == 2
    assert report.failed_count == 0
    assert report.all_passed is True
    assert report.failed_case_ids == ()
    assert report.suite == "unit"
    assert report.provider == "fake"
    assert report.model == "test-model"
    assert report.total_input_tokens == 6
    assert report.total_output_tokens == 4
    assert report.total_tokens == 10
    assert report.total_latency_ms >= 0
    assert report.total_cost_usd == pytest.approx(1.4e-5)
    payload = report.to_dict()
    assert set(EvalReport(()).to_dict()) < set(payload)
    assert payload["total"] == 2
    assert payload["all_passed"] is True
    assert payload["live"] is True
    assert [item["case_id"] for item in payload["metrics"]] == ["live-alpha", "live-beta"]
    for metric in report.metrics:
        assert metric.total_tokens == 5
        assert metric.cost_usd == pytest.approx(7e-6)
        assert metric.trace_ref is not None
        assert Path(metric.trace_ref).is_file()
        assert metric.session_ref is not None
        assert Path(metric.session_ref).is_file()
    assert len(provider.requests) == 2
    assert provider.requests[0].model == "test-model"


async def test_runner_keeps_trace_reference_for_failed_cases(tmp_path: Path) -> None:
    runner, _ = _runner(tmp_path, [_simple_response("unexpected answer")])
    cases = (
        EvalCase(
            id="live-failure",
            prompt="say the magic word",
            expect=EvalExpectation(final_text_contains=("banana",)),
        ),
    )
    report = await runner.run_cases(cases, suite="unit")
    assert report.passed_count == 0
    assert report.failed_case_ids == ("live-failure",)
    failures = report.failure_summary()
    assert len(failures) == 1
    assert failures[0].startswith("live-failure: final_text:")
    metric = report.metrics[0]
    assert metric.error is None
    assert metric.trace_ref is not None
    trace_lines = Path(metric.trace_ref).read_text(encoding="utf-8").splitlines()
    assert json.loads(trace_lines[0])["type"] == "run"


async def test_provider_error_becomes_a_scored_outcome(tmp_path: Path) -> None:
    runner, _ = _runner(tmp_path, [ProviderStreamError("provider exploded")])
    cases = (
        EvalCase(
            id="live-provider-error",
            prompt="explode",
            expect=EvalExpectation(error_contains=("provider exploded",), allows_error=True),
        ),
    )
    report = await runner.run_cases(cases, suite="unit")
    assert report.passed_count == 1
    outcome = report.results[0].outcome
    assert outcome is not None
    assert outcome.error == "ProviderStreamError: provider exploded"
    metric = report.metrics[0]
    assert metric.error == "ProviderStreamError: provider exploded"
    assert metric.trace_ref is not None
    assert Path(metric.trace_ref).is_file()


async def test_budget_stop_is_a_clean_scored_outcome(tmp_path: Path) -> None:
    over_budget = Usage(input_tokens=10, output_tokens=10, total_tokens=20)
    runner, _ = _runner(tmp_path, [_simple_response("ignored", usage=over_budget)])
    cases = (
        EvalCase(
            id="live-budget-token",
            prompt="say anything",
            budget=EvalBudget(max_tokens=5),
            expect=EvalExpectation(budget_exceeded=True),
        ),
    )
    report = await runner.run_cases(cases, suite="unit")
    assert report.all_passed is True
    outcome = report.results[0].outcome
    assert outcome is not None
    assert outcome.budget_exceeded is True
    assert outcome.error is None


def test_runner_validates_its_inputs(tmp_path: Path) -> None:
    provider = _ScriptedProvider([])
    with pytest.raises(EvalValidationError, match="LlmProvider"):
        LiveAgentRunner(
            provider=object(),  # type: ignore[arg-type]
            model="test-model",
            workdir=tmp_path,
            traces_root=tmp_path,
            sessions_root=tmp_path,
        )
    with pytest.raises(EvalValidationError, match="model"):
        LiveAgentRunner(
            provider=provider, model="", workdir=tmp_path, traces_root=tmp_path, sessions_root=tmp_path
        )
    with pytest.raises(EvalValidationError, match="max_tokens_per_call"):
        LiveAgentRunner(
            provider=provider,
            model="test-model",
            workdir=tmp_path,
            traces_root=tmp_path,
            sessions_root=tmp_path,
            max_tokens_per_call=0,
        )


async def test_runner_rejects_a_non_case(tmp_path: Path) -> None:
    runner, _ = _runner(tmp_path, [])
    with pytest.raises(EvalValidationError, match="EvalCase"):
        await runner.run("not-a-case")  # type: ignore[arg-type]


def test_metrics_and_report_validate_their_inputs() -> None:
    with pytest.raises(EvalValidationError, match="case_id"):
        LiveCaseMetrics(case_id="")
    with pytest.raises(EvalValidationError, match="cost_usd"):
        LiveCaseMetrics(case_id="live-case", cost_usd=-1.0)
    result = EvalReport(()).results
    with pytest.raises(EvalValidationError, match="metrics must match results"):
        LiveEvalReport(
            suite="unit",
            provider="fake",
            model="test-model",
            results=result,
            metrics=(LiveCaseMetrics(case_id="live-other"),),
            started_at="2026-01-02T03:04:05.000Z",
            finished_at="2026-01-02T03:04:06.000Z",
        )


def test_resolve_suite_finds_the_golden_suite() -> None:
    golden = resolve_suite("golden")
    assert golden.is_file()
    assert golden.name == "golden.jsonl"
    assert resolve_suite(str(golden)) == golden.resolve()


def test_cli_dry_run_lists_the_whole_suite(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "dry run: 20/20 case(s) from" in output
    assert "golden.jsonl" in output


def test_cli_dry_run_honours_filter_and_limit(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dry-run", "--filter", "memory"]) == 0
    output = capsys.readouterr().out
    assert "dry run: 5/20 case(s) from" in output
    assert "memory-search-provenance" in output
    assert main(["--dry-run", "--filter", "tool", "--limit", "2"]) == 0
    assert "dry run: 2/20 case(s) from" in capsys.readouterr().out


def test_cli_requires_an_explicit_live_opt_in(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--limit", "1"]) == 2
    captured = capsys.readouterr()
    assert "disabled by default" in captured.err
    assert "--live" in captured.err
    assert LIVE_ENV_VAR in captured.err


def test_cli_env_switch_enables_live_and_reports_missing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(LIVE_ENV_VAR, "1")
    assert main(["--limit", "1"], config=_config(api_key=None)) == 2
    captured = capsys.readouterr()
    assert "no API key" in captured.err
    assert TEST_API_KEY_ENV in captured.err


def test_cli_unknown_suite_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dry-run", "--suite", "no-such-suite"]) == 2
    captured = capsys.readouterr()
    assert "unknown eval suite" in captured.err
    assert "available suites: golden" in captured.err


def test_cli_no_matching_cases_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--live", "--filter", "zzz-no-such-case"]) == 2
    assert "no eval cases matched the selection" in capsys.readouterr().err


def test_cli_rejects_a_non_positive_limit(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dry-run", "--limit", "0"]) == 2
    assert "--limit must be a positive integer" in capsys.readouterr().err


def test_cli_runs_the_selected_cases_with_the_injected_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    suite = tmp_path / "suite.jsonl"
    case = EvalCase(
        id="live-cli-case",
        prompt="say hi",
        expect=EvalExpectation(final_text_contains=("hi",)),
    )
    suite.write_text(
        json.dumps({"case": case.to_dict(), "recorded": EvalOutcome(final_text="hi").to_dict()}) + "\n",
        encoding="utf-8",
    )
    provider = _ScriptedProvider([_simple_response("hi there")])
    report_path = tmp_path / "report.json"
    code = main(
        ["--live", "--suite", str(suite), "--json", str(report_path)],
        config=_config(),
        provider_factory=lambda _name, _config: provider,
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "live eval: 1/1 cases passed" in output
    assert f"wrote report to {report_path}" in output
    assert "OK: live eval passed" in output
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["all_passed"] is True
    assert payload["suite"] == "suite"
    assert payload["live"] is True
    assert payload["metrics"][0]["total_tokens"] == 5
    assert payload["metrics"][0]["cost_usd"] == pytest.approx(7e-6)
    assert Path(payload["metrics"][0]["trace_ref"]).is_file()
    assert Path(payload["metrics"][0]["session_ref"]).is_file()
    assert len(provider.requests) == 1
    assert provider.closed is True
