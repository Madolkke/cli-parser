"""The observed variants must preserve their matching product SDK requests."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from agentscope.model import OpenAIChatModel

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
    monkeypatch: pytest.MonkeyPatch,
    *,
    unpatched: bool,
    legacy: bool = False,
    thinking_chars: int = 240_000,
    visible_reply_chars: int = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    counters: dict[str, Any] = {}
    original_classes = builder.Agent, builder.ObservedOpenAIChatModel
    original_run = ablation.run_generation_phase

    @contextmanager
    def without_adapter(_: str) -> Iterator[ablation.AblationReport]:
        assert (builder.Agent, builder.ObservedOpenAIChatModel) == original_classes
        if legacy:

            class LegacyModel(original_classes[1]):
                count_tokens = OpenAIChatModel.count_tokens

            with patch.object(builder, "ObservedOpenAIChatModel", LegacyModel):
                yield ablation.AblationReport("legacy-unpatched")
        else:
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
        report = await ablation.offline_variant(
            "current" if legacy else "estimator",
            thinking_chars=thinking_chars,
            visible_reply_chars=visible_reply_chars,
        )
    assert (builder.Agent, builder.ObservedOpenAIChatModel) == original_classes
    return report, requests, counters


@pytest.mark.parametrize(
    ("legacy", "thinking_chars", "visible_reply_chars", "expected_summaries"),
    [
        (False, 240_000, 0, 0),
        (False, 0, 100_000, 1),
        (True, 240_000, 0, 1),
    ],
    ids=("product-thinking", "product-visible-compression", "legacy-thinking"),
)
async def test_adapter_matches_matching_product_sdk_requests_and_budgets(
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
    thinking_chars: int,
    visible_reply_chars: int,
    expected_summaries: int,
) -> None:
    settings = {
        "legacy": legacy,
        "thinking_chars": thinking_chars,
        "visible_reply_chars": visible_reply_chars,
    }
    baseline, baseline_requests, baseline_counters = await _capture_synthetic_run(
        monkeypatch, unpatched=True, **settings
    )
    observed, observed_requests, observed_counters = await _capture_synthetic_run(
        monkeypatch, unpatched=False, **settings
    )

    assert observed_requests == baseline_requests
    assert observed_counters == baseline_counters
    assert baseline["agents"] == []
    assert len(observed["agents"]) == 1
    assert observed["agents"][0]["compressions_completed"] == expected_summaries
    assert len(observed_requests) == 4 + expected_summaries
    assert observed["stage_calls"] == baseline["stage_calls"] == 4
    assert observed["summary_calls"] == baseline["summary_calls"] == expected_summaries
    assert observed["phase_completed"]
    assert observed_counters["agent_rounds"] == 4
    assert observed_counters["ttp_submissions"] == 2
    assert observed_counters["ttp_test_calls"] == 1
    assert observed_counters["terminal_reason"] == "success"
    if visible_reply_chars:
        triggers = [
            check
            for check in observed["agents"][0]["checks"]
            if check["compression_triggered"]
        ]
        assert len(triggers) == 1
        assert 38_400 <= triggers[0]["estimated_tokens_before"] < 48_000
    assert all(
        not check["reasoning_content_sent"] for check in observed["transport_checks"]
    )
    for field in (
        "phase_completed",
        "candidate_retained",
        "tests_used",
        "retained_candidate_submission_index",
        "compacted_interactions",
        "transport_checks",
    ):
        assert observed[field] == baseline[field]
