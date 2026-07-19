"""Deterministic, line-preserving sampling for model context."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

DEFAULT_MODEL_INPUT_CHAR_BUDGET: Final = 240_000
TRUNCATION_MARKER: Final = "[... middle omitted ...]\n"


@dataclass(frozen=True, slots=True)
class SampledCommandOutput:
    """One command output after applying its equal share of the model budget."""

    index: int
    text: str
    truncated: bool
    original_char_count: int
    allocated_char_budget: int

    @property
    def sampled_char_count(self) -> int:
        return len(self.text)


def _take_head_lines(lines: Sequence[str], budget: int) -> list[str]:
    selected: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) > budget:
            break
        selected.append(line)
        used += len(line)
    return selected


def _take_tail_lines(lines: Sequence[str], budget: int) -> list[str]:
    selected: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > budget:
            break
        selected.append(line)
        used += len(line)
    selected.reverse()
    return selected


def _truncate_on_line_boundaries(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    if budget <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:budget]

    source_budget = budget - len(TRUNCATION_MARKER)
    head_budget = source_budget * 3 // 4
    tail_budget = source_budget - head_budget
    lines = text.splitlines(keepends=True)

    head_lines = _take_head_lines(lines, head_budget)
    tail_lines = _take_tail_lines(lines[len(head_lines) :], tail_budget)
    head = "".join(head_lines)
    tail = "".join(tail_lines)

    # A single physical line can be larger than the entire model allocation. In
    # that case no complete line fits, so retain bounded character fragments rather
    # than returning a marker with no source evidence at all.
    if not head and head_budget:
        head = text[:head_budget]
    if not tail and tail_budget:
        tail = text[-tail_budget:]
    return head + TRUNCATION_MARKER + tail


def sample_command_outputs(
    command_outputs: Sequence[str],
    *,
    total_char_budget: int = DEFAULT_MODEL_INPUT_CHAR_BUDGET,
) -> list[SampledCommandOutput]:
    """Split the total budget equally and retain complete head/tail lines.

    Any indivisible remainder is assigned from the first output onward. Unused space
    from a short output is deliberately not reassigned, keeping sampling independent
    of input ordering beyond the documented remainder allocation.
    """

    if not command_outputs:
        raise ValueError("command_outputs must not be empty")
    if total_char_budget < 1:
        raise ValueError("total_char_budget must be positive")

    allocation, remainder = divmod(total_char_budget, len(command_outputs))
    sampled: list[SampledCommandOutput] = []
    for index, output in enumerate(command_outputs):
        budget = allocation + (1 if index < remainder else 0)
        text = _truncate_on_line_boundaries(output, budget)
        sampled.append(
            SampledCommandOutput(
                index=index,
                text=text,
                truncated=len(output) > budget,
                original_char_count=len(output),
                allocated_char_budget=budget,
            ),
        )
    return sampled


__all__ = [
    "DEFAULT_MODEL_INPUT_CHAR_BUDGET",
    "SampledCommandOutput",
    "TRUNCATION_MARKER",
    "sample_command_outputs",
]
