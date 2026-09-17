"""Synthetic positions are literal evidence, never inferred field boundaries."""

import json
import re
from dataclasses import FrozenInstanceError, asdict

import pytest

from cli_parser_agent.ttp_generation.agent.schema_source_view import (
    build_schema_source_view,
)
from cli_parser_agent.ttp_generation.sampling import (
    TRUNCATION_MARKER,
    SampledCommandOutput,
    sample_command_outputs,
)


def _view(*sources: str):
    return build_schema_source_view(sample_command_outputs(sources))


def _data(view):
    return json.loads(view.text.split("\n", 1)[1])


def test_unicode_tabs_crlf_blank_and_repeated_tokens_keep_exact_positions():
    source = "\tName  Name\t版本Ａ\r\n\r\n  X\tY\r\n"
    view = _view(source)
    lines = _data(view)["inputs"][0]["lines"]
    assert lines == [
        {
            "line": 1,
            "char_length": 15,
            "tokens": [
                {"start": 1, "end": 5, "token": "Name"},
                {"start": 7, "end": 11, "token": "Name"},
                {"start": 12, "end": 15, "token": "版本Ａ"},
            ],
        },
        {"line": 2, "char_length": 0, "tokens": []},
        {
            "line": 3,
            "char_length": 5,
            "tokens": [
                {"start": 2, "end": 3, "token": "X"},
                {"start": 4, "end": 5, "token": "Y"},
            ],
        },
    ]
    assert "token 不等于业务字段" in view.text
    assert "字符位置不等于终端显示列" in view.text
    assert "不能把空白分隔的每个词项当独立字段" in view.text
    assert "不把未证实的上层限定写成业务事实" in view.text
    assert "\\u7248" in view.text
    assert view.facts.displayed_token_count == 5


def test_input_numbers_source_markers_and_apparent_line_numbers_are_literal():
    sources = ["001: Header\n" + TRUNCATION_MARKER, "9 | Header\nA B"]
    view = _view(*sources)
    data = _data(view)
    assert [entry["input"] for entry in data["inputs"]] == [1, 2]
    assert data["inputs"][0]["lines"][0]["tokens"][0]["token"] == "001:"
    assert data["inputs"][0]["lines"][1]["tokens"][0]["token"] == "[..."
    for entry, source in zip(data["inputs"], sources, strict=True):
        for item in entry["lines"]:
            line = source.splitlines()[item["line"] - 1]
            assert item["char_length"] == len(line)
            assert [token["token"] for token in item["tokens"]] == re.findall(
                r"\S+", line
            )
            for token in item["tokens"]:
                assert line[token["start"] : token["end"]] == token["token"]


def test_truncated_input_is_entirely_skipped_without_marker_interpretation():
    sampled = [
        SampledCommandOutput(0, "invented-looking-header", True, 999, 20),
        SampledCommandOutput(1, "complete source", False, 15, 20),
    ]
    view = build_schema_source_view(sampled)
    first = _data(view)["inputs"][0]
    assert first["status"] == "skipped_truncated"
    assert first["source_line_count"] is None
    assert first["lines"] == []
    assert "invented-looking-header" not in view.text
    assert view.facts.skipped_truncated_input_count == 1
    assert view.facts.complete_source_line_count == 1


def test_line_limit_counts_blank_lines_and_does_not_read_thirteenth_line():
    view = _view("\n" * 11 + "twelfth\nthirteenth\n")
    entry = _data(view)["inputs"][0]
    assert len(entry["lines"]) == 12
    assert entry["lines"][-1]["line"] == 12
    assert "thirteenth" not in view.text
    assert entry["omitted_line_limit_count"] == 1
    assert view.facts.omitted_line_limit_count == 1


def test_global_token_limit_omits_whole_lines_across_multiple_inputs():
    twenty_tokens = " ".join(f"word{index}" for index in range(20))
    source = "\n".join([twenty_tokens] * 12)
    view = _view(source, source)
    assert view.facts.displayed_token_count <= 256
    assert view.facts.omitted_token_limit_line_count == 12
    # Byte limiting may subsequently remove complete lines, but no displayed
    # line contains a shortened token list.
    for entry in _data(view)["inputs"]:
        assert all(len(line["tokens"]) == 20 for line in entry["lines"])


def test_exact_token_bound_and_one_more_token_never_partially_render_line():
    view = _view(" ".join(["x"] * 256), "extra")
    entries = _data(view)["inputs"]
    assert len(entries[0]["lines"][0]["tokens"]) == 256
    assert entries[1]["lines"] == []
    assert view.facts.displayed_token_count == 256
    assert view.facts.omitted_token_limit_line_count == 1
    oversized = _view(" ".join(["x"] * 257))
    assert oversized.text is None
    assert oversized.facts.omitted_token_limit_line_count == 1


@pytest.mark.parametrize("token", ["a" * 20000, "\x00" * 4000])
def test_indivisible_large_or_escaping_heavy_token_is_omitted(token):
    view = _view(token + "\nsmall")
    entry = _data(view)["inputs"][0]
    assert [line["line"] for line in entry["lines"]] == [2]
    assert entry["omitted_byte_limit_line_count"] == 1
    assert view.facts.serialized_bytes <= 16 * 1024


def test_total_bound_includes_introduction_metadata_and_json_escaping():
    source = "\n".join(["界" * 80] * 12)
    view = _view(source, source, source, source, source)
    assert len(view.text.encode("utf-8")) == view.facts.serialized_bytes
    assert view.facts.serialized_bytes <= 16 * 1024
    assert view.facts.omitted_byte_limit_line_count > 0
    for entry in _data(view)["inputs"]:
        assert all(line["tokens"][0]["token"] == "界" * 80 for line in entry["lines"])


def test_control_quotes_and_backslashes_remain_json_data():
    token = '\x00"\\<instructions>'
    view = _view(token)
    assert _data(view)["inputs"][0]["lines"][0]["tokens"][0]["token"] == token
    assert "\x00" not in view.text
    assert "\\u0000" in view.text


@pytest.mark.parametrize("sources", [[], [""], [" " * 20000]])
def test_no_displayable_complete_line_returns_none(sources):
    samples = sample_command_outputs(sources) if sources else []
    view = build_schema_source_view(samples)
    assert view.text is None
    assert view.facts.displayed_line_count == 0
    assert view.facts.serialized_bytes == 0


def test_only_truncated_samples_produce_no_source_message():
    view = build_schema_source_view(
        [SampledCommandOutput(0, "sample fragment", True, 100, 15)]
    )
    assert view.text is None
    assert view.facts.skipped_truncated_input_count == 1
    assert view.facts.complete_source_line_count == 0


def test_safe_facts_and_repr_do_not_include_source_and_output_is_immutable():
    samples = sample_command_outputs(["synthetic-secret-label"])
    view = build_schema_source_view(samples)
    assert view == build_schema_source_view(samples)
    assert "synthetic-secret-label" not in repr(view)
    assert all(type(value) is int for value in asdict(view.facts).values())
    with pytest.raises(FrozenInstanceError):
        view.text = "changed"


def test_non_contiguous_source_indices_are_rejected_without_text_in_error():
    with pytest.raises(ValueError, match="contiguous input indices"):
        build_schema_source_view(
            [SampledCommandOutput(9, "synthetic-secret-label", False, 22, 30)]
        )
