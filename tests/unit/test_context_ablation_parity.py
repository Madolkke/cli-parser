"""The observed current variant must preserve the unpatched SDK requests."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import context_ablation as ablation  # noqa: E402

from cli_parser_agent.ttp_generation.agent import builder  # noqa: E402


class _CaptureSDKPatch:
    object = staticmethod(patch.object)

    def __init__(self, requests: list[dict[str, Any]]) -> None:
        self.requests = requests

    def __call__(self, target: str, completion: Any) -> Any:
        assert target == "openai.resources.chat.completions.AsyncCompletions.create"

        async def observed(*args: Any, **kwargs: Any) -> Any:
            self.requests.append(deepcopy(kwargs))
            return await completion(*args, **kwargs)

        return patch(target, observed)


async def _capture_synthetic_run(
    monkeypatch: pytest.MonkeyPatch, *, unpatched: bool
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    counters: dict[str, Any] = {}
    original_classes = builder.Agent, builder.ObservedOpenAIChatModel
    original_run = ablation.run_generation_phase

    @contextmanager
    def without_adapter(_: str) -> Iterator[ablation.AblationReport]:
        assert (builder.Agent, builder.ObservedOpenAIChatModel) == original_classes
        yield ablation.AblationReport("unpatched")

    async def observe_run(agent: Any, message: Any, session: Any, phase: str) -> Any:
        outcome = await original_run(agent, message, session, phase)
        counters.update(
            {
                name: getattr(session, name)
                for name in (
                    "agent_rounds",
                    "ttp_agent_rounds",
                    "ttp_submissions",
                    "ttp_test_calls",
                    "model_attempts_observed",
                    "model_retries_observed",
                    "max_agent_rounds",
                    "max_ttp_submissions",
                    "max_ttp_test_calls",
                    "terminal_reason",
                )
            }
        )
        return outcome

    with monkeypatch.context() as scoped:
        scoped.setattr(ablation, "patch", _CaptureSDKPatch(requests))
        scoped.setattr(ablation, "run_generation_phase", observe_run)
        if unpatched:
            scoped.setattr(ablation, "context_ablation", without_adapter)
        report = await ablation.offline_variant("current")
    assert (builder.Agent, builder.ObservedOpenAIChatModel) == original_classes
    return report, requests, counters


async def test_current_matches_unpatched_sdk_requests_and_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline, baseline_requests, baseline_counters = await _capture_synthetic_run(
        monkeypatch, unpatched=True
    )
    current, current_requests, current_counters = await _capture_synthetic_run(
        monkeypatch, unpatched=False
    )

    assert current_requests == baseline_requests
    assert current_counters == baseline_counters
    assert baseline["agents"] == []
    assert len(current["agents"]) == 1
    assert current["agents"][0]["compressions_completed"] == 1
    assert len(current_requests) == 5
    assert current["stage_calls"] == baseline["stage_calls"] == 4
    assert current["summary_calls"] == baseline["summary_calls"] == 1
    for field in (
        "phase_completed",
        "candidate_retained",
        "tests_used",
        "retained_candidate_submission_index",
        "compacted_interactions",
        "transport_checks",
    ):
        assert current[field] == baseline[field]
