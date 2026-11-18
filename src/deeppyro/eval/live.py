"""Live eval runner: score real provider runs without weakening the offline suite.

The eval package itself stays provider-free (see :mod:`deeppyro.eval`): neither
the golden fixture loader nor the offline runner imports a transport or opens a
socket.  This module is the explicit opt-in seam for real model calls.

:class:`LiveAgentRunner` drives the native :class:`~deeppyro.agent.loop.AgentLoop`
over one schema 1 session per case through an injected ``LlmProvider``, so it uses
the exact same provider abstraction, session log and trace store as a normal
DeepPyro turn.  Every case produces the same :class:`~deeppyro.eval.types.EvalResult`
the offline runner produces, plus a :class:`LiveCaseMetrics` record carrying
tokens, wall-clock latency, estimated cost and the trace/session file references
a failing case can be replayed from.

The ``python -m deeppyro.eval.live`` CLI keeps the network off by default: real
calls require ``--live`` or ``DEEPPYRO_EVAL_LIVE=1``, and a provider without a
resolved API key fails with a clear diagnostic instead of calling out.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from deeppyro.agent.budget import BudgetExceeded, BudgetLimits
from deeppyro.agent.events import NullAgentHooks, StepStarted, ToolCallStartedEvent
from deeppyro.agent.loop import AgentLoop
from deeppyro.config import Config, ModelProfile, ProviderConfig, load_config
from deeppyro.core.errors import ConfigError
from deeppyro.core.ids import new_session_id
from deeppyro.core.protocols import LlmProvider, Tool
from deeppyro.eval.errors import EvalFixtureError, EvalValidationError
from deeppyro.eval.fixture import load_eval_cases
from deeppyro.eval.runner import EvalRunner
from deeppyro.eval.types import EvalCase, EvalOutcome, EvalReport, EvalResult, ToolInvocation
from deeppyro.memory.backend import format_timestamp, utc_now
from deeppyro.observability.trace import TraceStore
from deeppyro.paths import sessions_dir
from deeppyro.session.schema import NativeSession

#: Process exit codes shared with the other DeepPyro command-line entry points.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

#: Environment switch that enables real provider calls without ``--live``.
LIVE_ENV_VAR = "DEEPPYRO_EVAL_LIVE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Default suite name resolved inside :data:`SUITE_DIR`.
DEFAULT_SUITE = "golden"
SUITE_DIR = "tests/fixtures/eval"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _arguments(raw: str) -> dict[str, object]:
    """Parse one serialized tool-call argument object, tolerating malformed JSON."""
    text = raw.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _positive_count(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise EvalValidationError(f"live eval {name} must be a non-negative integer")
    return value


def _non_negative_cost(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvalValidationError("live eval cost_usd must be a non-negative number")
    cost = float(value)
    if not math.isfinite(cost) or cost < 0:
        raise EvalValidationError("live eval cost_usd must be a non-negative number")
    return cost


@dataclass(frozen=True, slots=True)
class LiveCaseMetrics:
    """Accounting and replay references for one executed case."""

    case_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    steps: int = 0
    tool_calls: int = 0
    trace_ref: str | None = None
    session_ref: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id:
            raise EvalValidationError("live eval metrics case_id must be a non-empty string")
        _positive_count(self.input_tokens, "input_tokens")
        _positive_count(self.output_tokens, "output_tokens")
        _positive_count(self.latency_ms, "latency_ms")
        _positive_count(self.steps, "steps")
        _positive_count(self.tool_calls, "tool_calls")
        cost = _non_negative_cost(self.cost_usd)
        if cost != self.cost_usd:
            object.__setattr__(self, "cost_usd", cost)
        for name in ("trace_ref", "session_ref", "error"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise EvalValidationError(f"live eval metrics {name} must be a string or null")

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens for this case."""
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping with a stable key order."""
        return {
            "case_id": self.case_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "trace_ref": self.trace_ref,
            "session_ref": self.session_ref,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class LiveEvalReport:
    """A live eval report: offline ``EvalReport`` shape plus per-case metrics."""

    suite: str
    provider: str
    model: str
    results: tuple[EvalResult, ...]
    metrics: tuple[LiveCaseMetrics, ...]
    started_at: str
    finished_at: str
    live: bool = True

    def __post_init__(self) -> None:
        for name in ("suite", "provider", "model", "started_at", "finished_at"):
            if not isinstance(getattr(self, name), str):
                raise EvalValidationError(f"live eval report {name} must be a string")
        if not isinstance(self.live, bool):
            raise EvalValidationError("live eval report live must be a boolean")
        EvalReport(self.results)
        if not isinstance(self.metrics, tuple) or not all(
            isinstance(item, LiveCaseMetrics) for item in self.metrics
        ):
            raise EvalValidationError("live eval report metrics must be a tuple of LiveCaseMetrics")
        if tuple(item.case_id for item in self.metrics) != tuple(item.case_id for item in self.results):
            raise EvalValidationError("live eval report metrics must match results in case-id order")

    @property
    def total(self) -> int:
        """Number of cases in the report."""
        return len(self.results)

    @property
    def passed_count(self) -> int:
        """Number of passing cases."""
        return sum(1 for result in self.results if result.passed)

    @property
    def failed_count(self) -> int:
        """Number of failing cases."""
        return self.total - self.passed_count

    @property
    def all_passed(self) -> bool:
        """True when every case passed (an empty suite also passes)."""
        return self.failed_count == 0

    @property
    def failed_case_ids(self) -> tuple[str, ...]:
        """Ids of failing cases, in sorted order."""
        return tuple(result.case_id for result in self.results if not result.passed)

    @property
    def total_input_tokens(self) -> int:
        """Input tokens observed across every case."""
        return sum(metric.input_tokens for metric in self.metrics)

    @property
    def total_output_tokens(self) -> int:
        """Output tokens observed across every case."""
        return sum(metric.output_tokens for metric in self.metrics)

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens observed across every case."""
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_latency_ms(self) -> int:
        """Summed wall-clock latency of every case in milliseconds."""
        return sum(metric.latency_ms for metric in self.metrics)

    @property
    def total_cost_usd(self) -> float:
        """Estimated cost of the whole run in USD."""
        return sum(metric.cost_usd for metric in self.metrics)

    def failure_summary(self) -> tuple[str, ...]:
        """Return one deterministic human-readable line per failing case."""
        return EvalReport(self.results).failure_summary()

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping: offline report fields plus live extras."""
        payload = EvalReport(self.results).to_dict()
        payload["suite"] = self.suite
        payload["provider"] = self.provider
        payload["model"] = self.model
        payload["started_at"] = self.started_at
        payload["finished_at"] = self.finished_at
        payload["live"] = self.live
        payload["metrics"] = [metric.to_dict() for metric in self.metrics]
        return payload


class _CaseHooks(NullAgentHooks):
    """Collect native loop observations the eval outcome needs."""

    def __init__(self) -> None:
        self.steps = 0
        self.calls: list[ToolInvocation] = []

    def step_started(self, event: StepStarted) -> None:
        """Count model steps."""
        self.steps += 1

    def tool_call_started(self, event: ToolCallStartedEvent) -> None:
        """Record one dispatched tool call in invocation order."""
        self.calls.append(ToolInvocation(name=event.name, arguments=_arguments(event.arguments)))


class LiveAgentRunner:
    """AgentRunner that executes one eval case through the native agent loop.

    The runner never inspects ``case.expect``: expectations are only read by the
    scorer, exactly like every other AgentRunner.  Real transport is always an
    injected provider, so tests drive it with a scripted fake and stay offline.
    """

    def __init__(
        self,
        *,
        provider: LlmProvider,
        model: str,
        workdir: str | Path,
        traces_root: str | Path,
        sessions_root: str | Path,
        provider_name: str = "",
        profile: ModelProfile | None = None,
        tools: Iterable[Tool] = (),
        temperature: float | None = None,
        max_tokens_per_call: int | None = None,
    ) -> None:
        if not isinstance(provider, LlmProvider):
            raise EvalValidationError("live eval provider must implement the LlmProvider protocol")
        if not isinstance(model, str) or not model:
            raise EvalValidationError("live eval model must be a non-empty string")
        if not isinstance(provider_name, str):
            raise EvalValidationError("live eval provider_name must be a string")
        if profile is not None and not isinstance(profile, ModelProfile):
            raise EvalValidationError("live eval profile must be a ModelProfile or null")
        if temperature is not None and (
            isinstance(temperature, bool) or not isinstance(temperature, (int, float))
        ):
            raise EvalValidationError("live eval temperature must be a number or null")
        if max_tokens_per_call is not None and (
            type(max_tokens_per_call) is not int or max_tokens_per_call < 1
        ):
            raise EvalValidationError("live eval max_tokens_per_call must be a positive integer or null")
        self._provider = provider
        self._model = model
        self._provider_name = provider_name
        self._profile = profile
        self._tools = tuple(tools)
        self._workdir = Path(workdir)
        self._traces_root = Path(traces_root)
        self._sessions_root = Path(sessions_root)
        self._temperature = None if temperature is None else float(temperature)
        self._max_tokens_per_call = max_tokens_per_call
        self._metrics: dict[str, LiveCaseMetrics] = {}

    @property
    def provider_name(self) -> str:
        """Provider id recorded in the session log and traces."""
        return self._provider_name

    @property
    def model(self) -> str:
        """Provider model id used for every case."""
        return self._model

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        if self._profile is None:
            return 0.0
        return self._profile.estimate_cost(input_tokens=input_tokens, output_tokens=output_tokens)

    async def run(self, case: EvalCase) -> EvalOutcome:
        """Execute one case over a fresh schema 1 session and return its outcome."""
        if not isinstance(case, EvalCase):
            raise EvalValidationError("live eval case must be an EvalCase")
        session_id = new_session_id()
        session = NativeSession.create(
            self._sessions_root / f"{session_id}.jsonl",
            cwd=self._workdir,
            session_id=session_id,
        )
        hooks = _CaseHooks()
        loop = AgentLoop(
            session=session,
            provider=self._provider,
            model=self._model,
            tools=self._tools,
            cwd=self._workdir,
            hooks=hooks,
            budget=BudgetLimits(
                max_steps=case.budget.max_steps,
                max_tokens=case.budget.max_tokens,
                max_wall_seconds=case.budget.max_seconds,
            ),
            trace_store=TraceStore(self._traces_root),
            temperature=self._temperature,
            max_tokens_per_call=self._max_tokens_per_call,
            provider_name=self._provider_name or None,
        )
        outcome: EvalOutcome | None = None
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        steps = 0
        tool_calls = 0
        error: str | None = None
        started = time.monotonic()
        try:
            result = await loop.run(case.prompt)
        except BudgetExceeded:
            # A budget stop is a clean, scoreable outcome, not a crash: the loop
            # already persisted the abort, so no error text is attached.
            steps = hooks.steps
            tool_calls = len(hooks.calls)
            outcome = EvalOutcome(tool_calls=tuple(hooks.calls), steps=steps, budget_exceeded=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - runner failures must become scored outcomes
            steps = hooks.steps
            tool_calls = len(hooks.calls)
            error = f"{type(exc).__name__}: {exc}"
            outcome = EvalOutcome(tool_calls=tuple(hooks.calls), steps=steps, error=error)
        else:
            usage = result.usage
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            cost_usd = self._estimate_cost(input_tokens, output_tokens)
            steps = result.steps
            tool_calls = len(hooks.calls)
            outcome = EvalOutcome(
                final_text=result.final_text,
                tool_calls=tuple(hooks.calls),
                steps=result.steps,
                tokens=usage.total_tokens,
            )
        finally:
            latency_ms = max(0, int((time.monotonic() - started) * 1000))
            with contextlib.suppress(Exception):
                session.close()
            trace_ref = self._trace_ref(loop)
            self._metrics[case.id] = LiveCaseMetrics(
                case_id=case.id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                steps=steps,
                tool_calls=tool_calls,
                trace_ref=trace_ref,
                session_ref=str(session.path),
                error=error,
            )
        if outcome is None:  # pragma: no cover - defensive, the branches above always set it
            return EvalOutcome(error="live eval produced no outcome")
        return outcome

    def _trace_ref(self, loop: AgentLoop) -> str | None:
        run_id = loop.run_id
        if run_id is None:
            return None
        with contextlib.suppress(Exception):
            return str(TraceStore(self._traces_root).path_for(run_id))
        return None

    async def run_cases(
        self,
        cases: Sequence[EvalCase],
        *,
        timeout_seconds: float | None = None,
        suite: str = "",
    ) -> LiveEvalReport:
        """Run every case, score it with the offline scorer and attach metrics."""
        if not isinstance(suite, str):
            raise EvalValidationError("live eval suite must be a string")
        self._metrics = {}
        started_at = format_timestamp(utc_now())
        report = await EvalRunner(self, timeout_seconds=timeout_seconds).run_cases(cases)
        finished_at = format_timestamp(utc_now())
        metrics: list[LiveCaseMetrics] = []
        for result in report.results:
            metric = self._metrics.get(result.case_id)
            if metric is None:
                # The per-case timeout fired before the runner returned, so only
                # the scored error survived; keep the report shape intact.
                metric = LiveCaseMetrics(case_id=result.case_id, error=result.error)
            metrics.append(metric)
        return LiveEvalReport(
            suite=suite,
            provider=self._provider_name,
            model=self._model,
            results=report.results,
            metrics=tuple(metrics),
            started_at=started_at,
            finished_at=finished_at,
        )


def resolve_suite(name_or_path: str) -> Path:
    """Resolve a suite name or golden fixture path to an existing file."""
    if not isinstance(name_or_path, str) or not name_or_path.strip():
        raise EvalFixtureError("eval suite name must be a non-empty string")
    direct = Path(name_or_path)
    if direct.is_file():
        return direct.resolve()
    for root in (Path.cwd() / SUITE_DIR, _REPO_ROOT / SUITE_DIR):
        for candidate in (root / f"{name_or_path}.jsonl", root / name_or_path):
            if candidate.is_file():
                return candidate.resolve()
    available = sorted(path.stem for path in (_REPO_ROOT / SUITE_DIR).glob("*.jsonl"))
    listed = ", ".join(available) if available else "none"
    raise EvalFixtureError(f"unknown eval suite {name_or_path!r}; available suites: {listed}")


def _select_cases(
    cases: Sequence[EvalCase], *, filter_text: str | None, limit: int | None
) -> tuple[EvalCase, ...]:
    selected = list(cases)
    if filter_text:
        needle = filter_text.lower()
        selected = [
            case
            for case in selected
            if needle in case.id.lower()
            or needle in case.notes.lower()
            or any(needle in tag.lower() for tag in case.tags)
        ]
    if limit is not None:
        selected = selected[:limit]
    return tuple(selected)


def _profile_for(provider_config: ProviderConfig, model: str, provider_name: str) -> ModelProfile | None:
    for candidate in provider_config.models:
        if candidate.id == model:
            return ModelProfile.from_model_config(candidate, provider=provider_name)
    return None


def _env_live_enabled() -> bool:
    return os.environ.get(LIVE_ENV_VAR, "").strip().lower() in _TRUTHY


def _usage_error(message: str) -> int:
    print(f"live eval: {message}", file=sys.stderr)
    return EXIT_USAGE


def _default_provider_factory(name: str, provider_config: ProviderConfig) -> LlmProvider:
    """Build the real transport; imported lazily to keep offline imports socket-free."""
    from deeppyro.llm.registry import create_provider

    return create_provider(name, provider_config)


async def _run_live(
    *,
    provider: LlmProvider,
    provider_name: str,
    model: str,
    profile: ModelProfile | None,
    cases: Sequence[EvalCase],
    suite: str,
    timeout_seconds: float | None,
    workdir: Path,
    traces_root: Path,
    sessions_root: Path,
    temperature: float | None,
    max_tokens_per_call: int | None,
) -> LiveEvalReport:
    runner = LiveAgentRunner(
        provider=provider,
        provider_name=provider_name,
        model=model,
        profile=profile,
        workdir=workdir,
        traces_root=traces_root,
        sessions_root=sessions_root,
        temperature=temperature,
        max_tokens_per_call=max_tokens_per_call,
    )
    try:
        return await runner.run_cases(cases, timeout_seconds=timeout_seconds, suite=suite)
    finally:
        with contextlib.suppress(Exception):
            await provider.aclose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m deeppyro.eval.live",
        description="Run eval cases against a live provider; network calls are opt-in.",
    )
    parser.add_argument(
        "--suite", default=DEFAULT_SUITE, metavar="NAME", help="suite name or golden fixture path"
    )
    parser.add_argument("--provider", default=None, metavar="NAME", help="configured provider name")
    parser.add_argument("--model", default=None, metavar="ID", help="model id; defaults to config")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="score at most N cases")
    parser.add_argument(
        "--filter", dest="filter_text", default=None, metavar="TEXT", help="case id/tag/notes substring"
    )
    parser.add_argument(
        "--json", dest="json_path", default=None, metavar="PATH", help="write the JSON report to PATH"
    )
    parser.add_argument("--live", action="store_true", help="allow real provider calls")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="list selected cases only")
    parser.add_argument(
        "--timeout", type=float, default=None, metavar="SECONDS", help="per-case timeout override"
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    config: Config | None = None,
    provider_factory: Callable[[str, ProviderConfig], LlmProvider] | None = None,
) -> int:
    """Run the live eval CLI and return the process exit code."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else EXIT_OK
    if args.limit is not None and args.limit < 1:
        return _usage_error("--limit must be a positive integer")
    if args.timeout is not None and args.timeout <= 0:
        return _usage_error("--timeout must be a positive number")
    try:
        suite_path = resolve_suite(args.suite)
        cases = load_eval_cases(suite_path)
    except (EvalFixtureError, EvalValidationError) as exc:
        return _usage_error(str(exc))
    selected = _select_cases(cases, filter_text=args.filter_text, limit=args.limit)
    if args.dry_run:
        for case in selected:
            print(f"{case.id}\t{case.prompt[:80]}\t{','.join(case.tags)}")
        print(f"dry run: {len(selected)}/{len(cases)} case(s) from {suite_path}")
        return EXIT_OK
    if not (args.live or _env_live_enabled()):
        return _usage_error(
            f"real model calls are disabled by default; pass --live or set {LIVE_ENV_VAR}=1 to allow them"
        )
    if not selected:
        return _usage_error("no eval cases matched the selection")
    try:
        cfg = config if config is not None else load_config()
    except (ConfigError, OSError) as exc:
        return _usage_error(f"cannot load configuration: {exc}")
    provider_name = args.provider or cfg.agent.provider
    provider_config = cfg.providers.get(provider_name)
    if provider_config is None:
        configured = ", ".join(sorted(cfg.providers)) or "none"
        return _usage_error(f"provider {provider_name!r} is not configured (configured: {configured})")
    model = args.model or cfg.agent.model or (provider_config.models[0].id if provider_config.models else "")
    if not model:
        return _usage_error(f"no model configured for provider {provider_name!r}; pass --model")
    profile = _profile_for(provider_config, model, provider_name)
    if provider_config.resolve_api_key() is None:
        hint = provider_config.api_key_env or f"[providers.{provider_name}].api_key"
        return _usage_error(f"provider {provider_name!r} has no API key; set {hint}")
    factory = provider_factory if provider_factory is not None else _default_provider_factory
    try:
        provider = factory(provider_name, provider_config)
    except Exception as exc:  # noqa: BLE001 - any construction failure is a CLI diagnostic
        return _usage_error(f"cannot create provider {provider_name!r}: {exc}")
    report = asyncio.run(
        _run_live(
            provider=provider,
            provider_name=provider_name,
            model=model,
            profile=profile,
            cases=selected,
            suite=suite_path.stem,
            timeout_seconds=args.timeout,
            workdir=Path.cwd(),
            traces_root=TraceStore().root / "eval",
            sessions_root=sessions_dir() / "eval",
            temperature=cfg.agent.temperature,
            max_tokens_per_call=cfg.agent.max_tokens,
        )
    )
    if args.json_path is not None:
        target = Path(args.json_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            return _usage_error(f"cannot write report to {args.json_path}: {exc}")
        print(f"wrote report to {target}")
    print(
        f"live eval: {report.passed_count}/{report.total} cases passed "
        f"(suite={suite_path.stem!r} model={model!r})"
    )
    for line in report.failure_summary():
        print(f"FAILED {line}")
    print(
        f"live eval: tokens={report.total_tokens} latency={report.total_latency_ms / 1000:.3f}s "
        f"cost=${report.total_cost_usd:.6f}"
    )
    if report.all_passed:
        print("OK: live eval passed")
        return EXIT_OK
    print("FAILED: live eval")
    return EXIT_FAILURE


__all__ = [
    "DEFAULT_SUITE",
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_USAGE",
    "LIVE_ENV_VAR",
    "SUITE_DIR",
    "LiveAgentRunner",
    "LiveCaseMetrics",
    "LiveEvalReport",
    "main",
    "resolve_suite",
]


if __name__ == "__main__":
    raise SystemExit(main())
