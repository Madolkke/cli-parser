from __future__ import annotations

import pytest

from cli_parser_agent.ttp_generation.sampling import (
    TRUNCATION_MARKER,
    sample_command_outputs,
)


def test_short_outputs_are_returned_unchanged() -> None:
    sampled = sample_command_outputs(["one\n", "two\n"], total_char_budget=100)
    assert [item.text for item in sampled] == ["one\n", "two\n"]
    assert not any(item.truncated for item in sampled)
    assert [item.allocated_char_budget for item in sampled] == [50, 50]


def test_budget_is_split_evenly_with_remainder_assigned_in_input_order() -> None:
    sampled = sample_command_outputs(["x" * 100] * 3, total_char_budget=100)
    assert [item.allocated_char_budget for item in sampled] == [34, 33, 33]
    assert sum(item.sampled_char_count for item in sampled) <= 100


def test_truncation_keeps_complete_lines_with_three_to_one_head_tail_budget() -> None:
    lines = [f"{index:08d}\n" for index in range(30)]
    sampled = sample_command_outputs(["".join(lines)], total_char_budget=105)[0]

    head, tail = sampled.text.split(TRUNCATION_MARKER)
    assert head == "".join(lines[:6])
    assert tail == "".join(lines[-2:])
    assert sampled.truncated is True
    assert sampled.sampled_char_count <= sampled.allocated_char_budget
    assert head.endswith("\n")
    assert tail.endswith("\n")


def test_sampling_is_deterministic_and_does_not_redistribute_unused_budget() -> None:
    inputs = ["short\n", "line\n" * 100]
    first = sample_command_outputs(inputs, total_char_budget=100)
    second = sample_command_outputs(inputs, total_char_budget=100)
    assert first == second
    assert first[0].text == "short\n"
    assert first[1].allocated_char_budget == 50
    assert sum(item.sampled_char_count for item in first) < 100


def test_oversized_single_line_falls_back_to_bounded_character_fragments() -> None:
    source = "0123456789" * 20
    sampled = sample_command_outputs([source], total_char_budget=65)[0]
    head, tail = sampled.text.split(TRUNCATION_MARKER)

    source_budget = 65 - len(TRUNCATION_MARKER)
    expected_head_size = source_budget * 3 // 4
    expected_tail_size = source_budget - expected_head_size
    assert head == source[:expected_head_size]
    assert tail == source[-expected_tail_size:]
    assert sampled.sampled_char_count == 65


@pytest.mark.parametrize("outputs,budget", [([], 100), (["value"], 0)])
def test_sampling_rejects_invalid_inputs(outputs: list[str], budget: int) -> None:
    with pytest.raises(ValueError):
        sample_command_outputs(outputs, total_char_budget=budget)
