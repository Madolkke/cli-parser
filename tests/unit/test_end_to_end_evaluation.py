"""Generated contracts are scored independently of the manual field layout."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest
from agentscope import event as events
from test_dataset_registry import _load_runner, _write_dataset
from test_schema_consistency import fixtures

from cli_parser_agent import (
    ArtifactBundle,
    GenerationMetadata,
    GenerationPolicy,
    GenerationResult,
    TtpGeneratorSettings,
    ValidationIssue,
)
from cli_parser_agent.evaluation import (
    HarnessError,
    load_dataset_registry,
    preflight_dataset_registry,
    summarize_schema_review,
)
from cli_parser_agent.evaluation_parse_review import (
    PARSE_REVIEW_DIMENSIONS,
    summarize_parse_review,
)
from cli_parser_agent.ttp_generation.agent import PROMPT_VERSION


def schema():
    return {
        "type": "object",
        "properties": {
            "generated_name": {"type": "string", "description": "PRIVATE_DESCRIPTION"}
        },
        "additionalProperties": False,
    }


def tool_event(name, *, accepted=True, frozen=False, elapsed=1, candidate=None):
    output = {"accepted": accepted, "frozen": frozen, "issues": []}
    inputs = {}
    if name == "submit_result_schema":
        inputs["result_schema"] = candidate
    else:
        output["compiled_schema"] = candidate
    return events.CustomEvent(
        name="cli_parser.tool.result",
        metadata={"phase": "schema", "sensitive": True, "elapsed_seconds": elapsed},
        value={"tool_name": name, "input": inputs, "output": output},
    )


@pytest.mark.parametrize("tool", ["submit_result_schema", "submit_schema_plan"])
def test_frozen_observer_never_treats_pending_as_final(tool):
    tracer = _load_runner()._SchemaTracer(capture_frozen=True)
    tracer(tool_event(tool, candidate=schema()))
    assert tracer.frozen_schema is None
    assert tracer.facts()["first_frozen_seconds"] is None
    tracer(tool_event(tool, frozen=True, elapsed=2, candidate=schema()))
    assert tracer.frozen_schema == schema()
    assert tracer.facts()["first_frozen_seconds"] == 2
    assert "PRIVATE" not in json.dumps(tracer.facts())


def test_confirmation_freezes_without_inflating_submission_denominator():
    tracer = _load_runner()._SchemaTracer(capture_frozen=True)
    tracer(tool_event("submit_schema_plan", candidate=schema()))
    tracer(
        tool_event("confirm_schema_plan", frozen=True, elapsed=3, candidate=schema())
    )
    assert tracer.frozen_schema == schema()
    assert tracer.facts()["observed_submissions"] == 1
    assert tracer.facts()["observed_confirmations"] == 1
    assert tracer.facts()["first_frozen_seconds"] == 3


def test_compiler_observations_keep_only_allowlisted_integer_facts():
    tracer = _load_runner()._SchemaTracer()
    event = tool_event("submit_schema_plan", candidate=schema())
    event.value["output"]["facts"] = {
        "nodes": 4,
        "references": 10,
        "fallback_names": 1,
        "evidence_incomplete_nodes": 0,
        "required_fields": True,
        "schema": "PRIVATE",
        "content_hash": "PRIVATE",
    }
    tracer(event)
    assert tracer.facts()["compiler_facts"] == [
        {
            "nodes": 4,
            "references": 10,
            "fallback_names": 1,
            "evidence_incomplete_nodes": 0,
        }
    ]
    assert "PRIVATE" not in json.dumps(tracer.facts())


async def test_end_to_end_runner_uses_generate_and_never_manual_expected(
    tmp_path, monkeypatch
):
    runner = _load_runner()
    registry = load_dataset_registry(_write_dataset(tmp_path, complete=True))
    reports = preflight_dataset_registry(registry)
    root = tmp_path / "output"
    policy = GenerationPolicy()
    monkeypatch.setattr(
        runner,
        "_configuration",
        lambda: (
            TtpGeneratorSettings(api_key="offline", model_name="offline"),
            policy,
            root,
            {},
        ),
    )
    calls = []
    active = peak = 0

    class Generator:
        def __init__(self, **kwargs):
            assert kwargs["policy"] is policy

        async def generate(self, request, *, observer):
            nonlocal active, peak
            calls.append(request.model_dump())
            active += 1
            peak = max(peak, active)
            observer(
                tool_event("submit_result_schema", frozen=True, candidate=schema())
            )
            await asyncio.sleep(0.01)
            active -= 1
            return GenerationResult(
                status="success",
                artifact=ArtifactBundle(
                    result_schema=schema(),
                    ttp_template="Value: {{ generated_name }}",
                    records=[{"generated_name": "alpha"}],
                ),
                metadata=GenerationMetadata(
                    model_name="offline",
                    command_output_count=1,
                    schema_submissions=1,
                    schema_agent_rounds=1,
                    ttp_agent_rounds=2,
                    agent_rounds=3,
                    laminar_trace_id=str(UUID(int=len(calls))),
                ),
            )

    monkeypatch.setattr(runner, "TtpGenerator", Generator)
    args = runner._build_parser().parse_args(
        [
            "run",
            "--registry",
            str(registry.path),
            "--mode",
            "end-to-end",
            "--trials",
            "4",
            "--concurrency",
            "2",
            "--trace-rounds",
        ]
    )
    assert await runner._run_schema(args, registry, reports) == 0
    assert peak == 2
    assert calls == [{"command_outputs": ["Value: alpha\n"]}] * 4
    summary = json.loads(next(root.glob("*/summary.json")).read_text())
    assert summary["mode"] == "end-to-end"
    assert summary["generation_success_count"] == 4
    assert summary["schema_generation_success_count"] == 4
    assert summary["independent_acceptance_count"] == 4
    assert summary["contract_consistency"]["demo.case"]["pair_count"] == 6
    text = "".join(p.read_text() for p in root.rglob("*") if p.is_file())
    for forbidden in [
        "generated_name",
        "PRIVATE_DESCRIPTION",
        "Value: alpha",
        "strict_pass",
        "records_exact",
        "signature",
        "sha256",
    ]:
        assert forbidden not in text


@pytest.mark.parametrize("frozen", [True, False])
async def test_ttp_failure_keeps_only_confirmed_schema(monkeypatch, frozen):
    runner = _load_runner()

    class Generator:
        def __init__(self, **kwargs):
            pass

        async def generate(self, request, *, observer):
            observer(
                tool_event("submit_schema_plan", frozen=frozen, candidate=schema())
            )
            return GenerationResult(
                status="failed",
                issues=[ValidationIssue(code="generation.timeout", message="timeout")],
                metadata=GenerationMetadata(
                    model_name="offline",
                    command_output_count=1,
                    schema_submissions=1,
                    termination_reason="generation_timeout",
                ),
            )

    monkeypatch.setattr(runner, "TtpGenerator", Generator)
    case = SimpleNamespace(inputs=[SimpleNamespace(text="private")], schema=schema())
    document, proposal = await runner._run_end_to_end_trial(
        case, None, GenerationPolicy(), runner._SchemaTracer(capture_frozen=True)
    )
    assert document["generation_success"] is False
    assert document["schema_generation_success"] is frozen
    assert (proposal is not None) is frozen
    assert document["independent_acceptance"]["valid"] is False


@pytest.mark.parametrize("failure", ["cancelled", "exception"])
async def test_end_to_end_failures_do_not_export_exception_text(monkeypatch, failure):
    runner = _load_runner()

    class Generator:
        def __init__(self, **kwargs):
            pass

        async def generate(self, request, *, observer):
            if failure == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("PRIVATE exception")

    monkeypatch.setattr(runner, "TtpGenerator", Generator)
    case = SimpleNamespace(inputs=[SimpleNamespace(text="private")], schema=schema())
    call = runner._run_end_to_end_trial(
        case, None, GenerationPolicy(), runner._SchemaTracer(capture_frozen=True)
    )
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        document, proposal = await call
        assert proposal is None
        assert document["exception_type"] == "RuntimeError"
        assert document["independent_acceptance"] is None
        assert "PRIVATE" not in json.dumps(document)


@pytest.mark.parametrize(
    "options",
    [
        ["--mode", "end-to-end", "--write-baseline", "baseline.json"],
    ],
)
def test_invalid_experiment_configuration_is_rejected_before_loading(tmp_path, options):
    assert (
        _load_runner().main(
            ["run", "--registry", str(tmp_path / "absent.toml"), *options]
        )
        == 2
    )


def review_fixtures():
    summary, schema_review = fixtures()
    summary["mode"] = "end-to-end"
    for trial in summary["trials"]:
        trial["schema_generation_success"] = True
        trial["independent_acceptance"] = {"valid": True}
    parse_review = {
        "review_version": 1,
        "run_id": summary["run_id"],
        "trials": [
            {
                "trial_id": trial["trial_id"],
                "case_id": trial["case_id"],
                "trace_id": trial["trace_id"],
                "span_ids": [],
                "dimensions": dict.fromkeys(PARSE_REVIEW_DIMENSIONS, "passed"),
                "overall": "acceptable",
                "categories": [],
                "paths": [],
            }
            for trial in summary["trials"]
        ],
    }
    return summary, schema_review, parse_review


def test_parse_review_combines_schema_execution_and_content_conservatively():
    summary, schema_review, review = review_fixtures()
    result = summarize_parse_review(summary, review, schema_review)
    assert result["end_to_end_joint_pass_count"] == 4
    assert result["review_complete"]
    # Structurally identical but semantically bad schemas cannot pass jointly.
    for row in schema_review["trials"]:
        row["overall"] = "needs_revision"
        row["dimensions"]["coverage"] = "issue"
    for pair in schema_review["pairs"]:
        pair["judgment"] = "at_least_one_issue"
    result = summarize_parse_review(summary, review, schema_review)
    assert result["end_to_end_joint_pass_count"] == 0
    review["trials"].pop()
    assert (
        summarize_parse_review(summary, review, schema_review)["missing_trial_reviews"]
        == 1
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "trace",
        "case",
        "extra",
        "path",
        "long",
        "count",
        "category",
        "unknown",
        "failed",
        "independent",
        "waived",
    ],
)
def test_parse_review_rejects_mismatches_and_body_leaks(mutation):
    summary, schema_review, review = review_fixtures()
    row = review["trials"][0]
    if mutation == "duplicate":
        review["trials"][1] = deepcopy(row)
    if mutation == "trace":
        row["trace_id"] = str(UUID(int=999))
    if mutation == "case":
        row["case_id"] = "different"
    if mutation == "extra":
        row["body"] = "PRIVATE"
    if mutation == "path":
        row["paths"] = ["/private secret"]
    if mutation == "long":
        row["paths"] = ["/" + "a" * 2048]
    if mutation == "count":
        row["paths"] = [f"/field{i}" for i in range(25)]
    if mutation == "category":
        row["categories"] = ["PRIVATE"]
    if mutation == "unknown":
        row["dimensions"]["coverage"] = "insufficient_evidence"
    if mutation == "failed":
        summary["trials"][0]["generation_success"] = False
    if mutation == "independent":
        summary["trials"][0]["independent_acceptance"] = None
    if mutation == "waived":
        row["dimensions"]["coverage"] = "not_applicable"
    with pytest.raises(HarnessError):
        summarize_parse_review(summary, review, schema_review)


@pytest.mark.parametrize("contradiction", ["category", "path", "uncategorized_issue"])
def test_parse_review_rejects_contradictory_acceptance_or_unclassified_issue(
    contradiction,
):
    summary, schema_review, review = review_fixtures()
    row = review["trials"][0]
    if contradiction == "category":
        row["categories"] = ["execution_failure", "coverage"]
    elif contradiction == "path":
        row["paths"] = ["/name"]
    else:
        row["overall"] = "needs_revision"
        row["dimensions"]["coverage"] = "issue"
    with pytest.raises(HarnessError):
        summarize_parse_review(summary, review, schema_review)


def test_categorized_parse_issue_cannot_pass_jointly_with_acceptable_schema():
    summary, schema_review, review = review_fixtures()
    row = review["trials"][0]
    row["overall"] = "needs_revision"
    row["dimensions"]["coverage"] = "issue"
    row["categories"] = ["coverage"]
    row["paths"] = ["/name"]
    result = summarize_parse_review(summary, review, schema_review)
    assert result["end_to_end_joint_pass_count"] == 3
    assert result["needs_revision_trials"] == 1
    assert result["review_complete"] is True


def test_schema_review_keeps_frozen_schema_when_ttp_failed():
    summary, schema_review, _ = review_fixtures()
    summary["trials"][0]["generation_success"] = False
    result = summarize_schema_review(summary, schema_review)
    assert result["acceptable_trials"] == 4
    assert result["confirmed_reasonable_consistent_pairs"] == 6


def test_parse_review_accepts_schema_policy_v2_without_changing_joint_semantics():
    from test_schema_consistency import policy_fixtures

    summary, legacy, parse_review = review_fixtures()
    _, schema_review = policy_fixtures()
    schema_review["trials"][0]["policy_checks"]["source_label_fidelity"] = "issue"
    schema_review["trials"][0]["policy_paths"] = ["/title"]
    policy_summary = summarize_schema_review(summary, schema_review)
    assert policy_summary["policy_compliance"]["passed_trials"] == 3
    result = summarize_parse_review(summary, parse_review, schema_review)
    old_result = summarize_parse_review(summary, parse_review, legacy)
    assert result.pop("schema") == policy_summary
    old_result.pop("schema")
    assert result == old_result


def test_parse_review_cli_is_offline_and_preserves_sources(tmp_path, monkeypatch):
    runner = _load_runner()
    summary, schema_review, parse_review = review_fixtures()
    for name, value in [
        ("summary", summary),
        ("schema", schema_review),
        ("parse", parse_review),
    ]:
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    before = (tmp_path / "summary.json").read_bytes()
    monkeypatch.setattr(
        runner, "_configuration", lambda: pytest.fail("offline required")
    )
    assert (
        runner.main(
            [
                "parse-review",
                "--run-directory",
                str(tmp_path),
                "--review-file",
                str(tmp_path / "parse.json"),
                "--schema-review-file",
                str(tmp_path / "schema.json"),
            ]
        )
        == 0
    )
    output = json.loads((tmp_path / "parse-review-summary.json").read_text())
    assert output["end_to_end_joint_pass_count"] == 4
    assert (tmp_path / "summary.json").read_bytes() == before


async def test_default_trials_share_snapshot_and_global_concurrency(
    tmp_path, monkeypatch
):
    runner = _load_runner()
    registry = load_dataset_registry(_write_dataset(tmp_path, complete=True))
    reports = preflight_dataset_registry(registry)
    root = tmp_path / "experiment"
    monkeypatch.setattr(
        runner,
        "_configuration",
        lambda: (
            TtpGeneratorSettings(api_key="offline", model_name="offline"),
            GenerationPolicy(),
            root,
            {"prompt": {"version": PROMPT_VERSION}},
        ),
    )
    active = peak = 0
    started = []
    input_identities = set()

    async def trial(case, settings, policy, tracer):
        nonlocal active, peak
        started.append(case.id)
        input_identities.add(id(case.inputs))
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"generation_success": True, "proposal_revalidated": True}, schema()

    monkeypatch.setattr(runner, "_run_schema_trial", trial)
    args = runner._build_parser().parse_args(
        [
            "run",
            "--registry",
            str(registry.path),
            "--mode",
            "schema-only",
            "--trials",
            "4",
            "--concurrency",
            "4",
        ]
    )
    assert await runner._run_schema(args, registry, reports) == 0
    assert peak == 4
    assert len(input_identities) == 1
    assert len(started) == 4
    assert len(set(started)) == 1
    summaries = [json.loads(p.read_text()) for p in root.rglob("summary.json")]
    assert len(summaries) == 1
    assert summaries[0]["trial_count"] == 4
    assert summaries[0]["configuration"]["prompt"]["version"] == PROMPT_VERSION
    assert len({t["trial_id"] for t in summaries[0]["trials"]}) == 4
    assert not list(root.rglob("experiment.json"))
