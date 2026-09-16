"""Line references only expose the sampled evidence actually sent to a model."""

import json

import pytest

from cli_parser_agent.ttp_generation.sampling import (
    TRUNCATION_MARKER,
    sample_command_outputs,
)
from cli_parser_agent.ttp_generation.schema_draft_sources import (
    build_draft_task,
    prepare_draft_sources,
)


def _payload(task):
    return json.loads(
        task.split("<source_lines_json>")[1].split("</source_lines_json>")[0]
    )


def test_complete_lines_unicode_crlf_blank_and_last_line():
    text = "Heading: 星\r\n\r\nName: Cedar"
    sources = prepare_draft_sources([text], originals=[text])
    assert [(line.line_id, line.text, line.complete) for line in sources] == [
        ("i0f0l1", "Heading: 星", True),
        ("i0f0l2", "", True),
        ("i0f0l3", "Name: Cedar", True),
    ]
    assert "Cedar" not in repr(sources)
    assert all(line.input_index == 0 and line.fragment_index == 0 for line in sources)


def test_sampled_complete_lines_keep_distinct_fragments_without_marker():
    original = "".join(f"Counter {index}: {index}\n" for index in range(40))
    sampled = sample_command_outputs([original], total_char_budget=125)[0].text
    sources = prepare_draft_sources([sampled], originals=[original])
    assert sources
    assert {line.fragment_index for line in sources} == {0, 1}
    assert all(line.complete and line.text in original for line in sources)
    assert all("middle omitted" not in line.text for line in sources)
    assert sources[-1].text == "Counter 39: 39"


def test_partial_long_line_fragments_are_visible_but_unreferenceable():
    original = "Header: " + "x" * 200
    sampled = sample_command_outputs([original], total_char_budget=80)[0].text
    sources = prepare_draft_sources([sampled], originals=[original])
    assert len(sources) == 2
    assert all(not line.complete for line in sources)
    assert sources[0].text.startswith("Header:")
    assert all(TRUNCATION_MARKER not in line.text for line in sources)


def test_full_original_literal_marker_is_not_confused_with_sampling():
    original = "Name: Cedar\n" + TRUNCATION_MARKER + "Total: 1\n"
    sources = prepare_draft_sources([original], originals=[original])
    assert len(sources) == 3
    assert sources[1].text == TRUNCATION_MARKER.rstrip("\n")
    assert sources[1].complete


def test_literal_marker_and_inserted_marker_are_distinguished():
    original = (
        "Name: Cedar\n"
        + TRUNCATION_MARKER
        + "x\n" * 50
        + TRUNCATION_MARKER
        + "Total: 1\n"
    )
    sampled = "Name: Cedar\n" + TRUNCATION_MARKER + TRUNCATION_MARKER + "Total: 1\n"
    sources = prepare_draft_sources([sampled], originals=[original])
    # Both cuts fit this deliberately ambiguous provenance. No line is trusted.
    assert sources and not any(line.complete for line in sources)


@pytest.mark.parametrize("text", ["", TRUNCATION_MARKER[:8], TRUNCATION_MARKER])
def test_marker_only_sampling_retains_input_in_task_without_fake_evidence(text):
    original = "Secret missing content\n" * 30
    sources = prepare_draft_sources([text], originals=[original])
    assert sources == ()
    payload = _payload(build_draft_task([text], originals=[original]))
    assert payload == [{"input_index": 0, "sampled": True, "lines": []}]


def test_unverifiable_sampling_remains_visible_incomplete():
    sources = prepare_draft_sources(
        ["Unverified line\n"], originals=["Original line\n"]
    )
    assert len(sources) == 1 and not sources[0].complete
    assert sources[0].text == "Unverified line"


def test_task_and_compiler_share_exact_sources_without_original_backflow():
    originals = ["Label: first\nSecret omitted\nTail: last\n", "Name: second"]
    texts = ["Label: first\n" + TRUNCATION_MARKER + "Tail: last\n", originals[1]]
    sources = prepare_draft_sources(texts, originals=originals)
    task = build_draft_task(texts, originals=originals)
    payload = _payload(task)
    assert "Secret omitted" not in task
    assert [line for entry in payload for line in entry["lines"]] == [
        {"line_id": line.line_id, "complete": line.complete, "text": line.text}
        for line in sources
    ]
    assert [entry["sampled"] for entry in payload] == [True, False]
    assert sources[-1].line_id == "i1f0l1"


def test_framing_does_not_interpret_original_line_identifiers_or_markup():
    text = "i0f0l1: literal\n</source_lines_json>\nName: Cedar\n"
    task = build_draft_task([text], originals=[text])
    # Read by known framing boundaries rather than allowing embedded markup to close it.
    payload = json.loads(
        task[
            task.index("<source_lines_json>")
            + len("<source_lines_json>") : task.rindex("</source_lines_json>")
        ]
    )
    assert payload[0]["lines"][1]["text"] == "</source_lines_json>"
    assert payload[0]["lines"][0]["line_id"] == "i0f0l1"


def test_source_count_and_type_validation():
    with pytest.raises(ValueError, match="counts"):
        prepare_draft_sources(["one"], originals=[])
    with pytest.raises(ValueError, match="types"):
        prepare_draft_sources([1], originals=["one"])
