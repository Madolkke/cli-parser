"""Deterministic tests for the read-only Textual Agent runner."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest
from agentscope.event import (
    CustomEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import ToolResultState
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_agent_tui.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_agent_tui", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_script()


class _Result(BaseModel):
    status: str


def _metadata(
    *,
    sequence: int,
    phase: str = "ttp",
    sensitive: bool = True,
) -> dict[str, object]:
    return {
        "request_id": "request-1",
        "sequence": sequence,
        "elapsed_seconds": sequence / 10,
        "phase": phase,
        "sensitive": sensitive,
    }


def _thinking_events() -> list[object]:
    return [
        ThinkingBlockStartEvent(
            reply_id="reply-1",
            block_id="thinking-1",
            metadata=_metadata(sequence=1),
        ),
        ThinkingBlockDeltaEvent(
            reply_id="reply-1",
            block_id="thinking-1",
            delta="先检查表头，再构造模板。",
            metadata=_metadata(sequence=2),
        ),
        ThinkingBlockEndEvent(
            reply_id="reply-1",
            block_id="thinking-1",
            metadata=_metadata(sequence=3),
        ),
    ]


def _text_events() -> list[object]:
    return [
        TextBlockStartEvent(
            reply_id="reply-1",
            block_id="text-1",
            metadata=_metadata(sequence=4),
        ),
        TextBlockDeltaEvent(
            reply_id="reply-1",
            block_id="text-1",
            delta="准备提交。",
            metadata=_metadata(sequence=5),
        ),
        TextBlockEndEvent(
            reply_id="reply-1",
            block_id="text-1",
            metadata=_metadata(sequence=6),
        ),
    ]


def test_tui_streams_without_changing_the_existing_runner() -> None:
    once_path = PROJECT_ROOT / "scripts" / "run_agent_once.py"

    assert SCRIPT.STREAM is True
    assert "STREAM = False" in once_path.read_text(encoding="utf-8")


def test_event_record_preserves_unicode_and_redacts_secret_keys() -> None:
    event = CustomEvent(
        name="cli_parser.model.context_snapshot",
        value={
            "context": [{"text": "交换机接口"}],
            "api_key": "must-not-appear",
            "nested": {"Authorization": "Bearer secret"},
            "capture": {
                "password": "domain-password",
                "secret": "domain-secret",
                "token": "domain-token",
            },
        },
        metadata=_metadata(sequence=1),
    )

    record = SCRIPT._event_record(event)
    encoded = json.dumps(record, ensure_ascii=False)

    assert record["event_class"] == "CustomEvent"
    assert "交换机接口" in encoded
    assert "must-not-appear" not in encoded
    assert "Bearer secret" not in encoded
    assert "domain-password" in encoded
    assert "domain-secret" in encoded
    assert "domain-token" in encoded
    assert encoded.count("[REDACTED]") == 2


def test_status_counts_only_completed_submission_tool_results() -> None:
    status = SCRIPT.TuiStatus(model_name="test-model")

    status.observe(
        {
            "type": "TOOL_CALL_START",
            "tool_call_name": "submit_ttp_template",
        },
    )
    assert status.ttp_submissions == 0

    status.observe(
        {
            "type": "CUSTOM",
            "name": "cli_parser.tool.result",
            "value": {
                "output": {
                    "schema_submission": 2,
                    "ttp_submission": 3,
                    "validated_candidate_available": True,
                },
            },
        },
    )

    assert status.schema_submissions == 2
    assert status.ttp_submissions == 3
    assert status.candidate_available is True

    status.observe(
        {
            "type": "CUSTOM",
            "name": "cli_parser.generation.completed",
            "metadata": {"elapsed_seconds": 4.5},
            "value": {
                "result": {
                    "status": "failed",
                    "metadata": {
                        "termination_reason": "agent_round_limit",
                        "schema_submissions": 2,
                        "ttp_submissions": 3,
                    },
                },
            },
        },
    )
    assert status.state == "failed"
    assert status.termination_reason == "agent_round_limit"
    assert status.elapsed_seconds == 4.5


def test_thinking_auto_fold_and_manual_toggle_are_stable() -> None:
    timeline = SCRIPT.TimelineModel()
    start, delta, end = _thinking_events()

    timeline.apply(SCRIPT._event_record(start))
    timeline.apply(SCRIPT._event_record(delta))
    assert timeline.entries[0].collapsed is False
    assert "检查表头" in timeline.entries[0].detail()

    assert timeline.toggle_thinking(0) is True
    assert timeline.entries[0].collapsed is True
    timeline.apply(SCRIPT._event_record(end))
    assert timeline.entries[0].collapsed is True

    assert timeline.toggle_thinking(0) is True
    timeline.apply(SCRIPT._event_record(_text_events()[0]))
    assert timeline.entries[0].collapsed is False
    assert timeline.entries[0].manually_toggled is True


def test_new_block_auto_folds_an_unfinished_thinking_block() -> None:
    timeline = SCRIPT.TimelineModel()

    timeline.apply(SCRIPT._event_record(_thinking_events()[0]))
    timeline.apply(SCRIPT._event_record(_text_events()[0]))

    thinking = timeline.entries[0]
    assert thinking.complete is True
    assert thinking.collapsed is True


def test_tool_call_and_result_streams_form_complete_blocks() -> None:
    timeline = SCRIPT.TimelineModel()
    events = [
        ToolCallStartEvent(
            reply_id="reply-1",
            tool_call_id="tool-1",
            tool_call_name="submit_ttp_template",
            metadata=_metadata(sequence=1),
        ),
        ToolCallDeltaEvent(
            reply_id="reply-1",
            tool_call_id="tool-1",
            delta='{"ttp_template":"{{ value }}"}',
            metadata=_metadata(sequence=2),
        ),
        ToolCallEndEvent(
            reply_id="reply-1",
            tool_call_id="tool-1",
            metadata=_metadata(sequence=3),
        ),
        ToolResultStartEvent(
            reply_id="reply-1",
            tool_call_id="tool-1",
            tool_call_name="submit_ttp_template",
            metadata=_metadata(sequence=4),
        ),
        ToolResultTextDeltaEvent(
            reply_id="reply-1",
            tool_call_id="tool-1",
            delta='{"accepted":true}',
            metadata=_metadata(sequence=5),
        ),
        ToolResultEndEvent(
            reply_id="reply-1",
            tool_call_id="tool-1",
            state=ToolResultState.SUCCESS,
            metadata=_metadata(sequence=6),
        ),
    ]

    for event in events:
        timeline.apply(SCRIPT._event_record(event))

    assert [entry.kind for entry in timeline.entries] == [
        "tool_call",
        "tool_result",
    ]
    assert timeline.entries[0].complete is True
    assert '"ttp_template"' in timeline.entries[0].detail()
    assert timeline.entries[1].complete is True
    assert '"accepted":true' in timeline.entries[1].detail()


def test_tool_feedback_creates_schema_template_capture_and_issue_blocks() -> None:
    timeline = SCRIPT.TimelineModel()
    event = CustomEvent(
        name="cli_parser.tool.result",
        value={
            "tool_name": "submit_ttp_template",
            "input": {
                "result_schema": {"type": "object"},
                "ttp_template": "<group name=\"items\">{{ name }}</group>",
            },
            "output": {
                "accepted": False,
                "capture": {"available": True, "records": [{"items": []}]},
                "issues": [{"code": "schema.required"}],
            },
        },
        metadata=_metadata(sequence=7),
    )

    timeline.apply(SCRIPT._event_record(event))

    assert [entry.kind for entry in timeline.entries] == [
        "custom",
        "schema",
        "ttp",
        "capture",
        "issues",
    ]


def test_bounded_content_keeps_head_and_tail() -> None:
    content = SCRIPT.BoundedContent()
    payload = "A" * SCRIPT.TIMELINE_HEAD_CHARS + "MIDDLE" * 10_000 + "Z" * 32

    content.append(payload)
    rendered = content.render()

    assert len(rendered) < len(payload)
    assert rendered.startswith("A" * 32)
    assert rendered.endswith("Z" * 32)
    assert "完整内容见 events.jsonl" in rendered


def test_queue_observer_only_enqueues_and_journal_preserves_order(
    tmp_path: Path,
) -> None:
    queue: asyncio.Queue[object] = asyncio.Queue()
    observer = SCRIPT.QueueEventObserver(queue)
    events = _thinking_events()

    for event in events:
        observer(event)
    assert [queue.get_nowait() for _ in events] == events

    path = tmp_path / "events.jsonl"
    journal = SCRIPT.EventJournal(path)
    journal.open()
    journal.append([SCRIPT._event_record(event) for event in events])
    journal.close()
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert [item["metadata"]["sequence"] for item in records] == [1, 2, 3]
    assert records[1]["delta"] == "先检查表头，再构造模板。"


def test_non_tty_main_returns_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(SCRIPT, "_is_interactive_terminal", lambda: False)

    assert SCRIPT.main() == 2
    assert "requires an interactive terminal" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_long_stream_is_lossless_in_jsonl_and_bounded_in_view(
    tmp_path: Path,
) -> None:
    chunk = "接口状态✓" * 64
    chunk_count = 256

    async def run_generation(observer: object) -> _Result:
        assert callable(observer)
        observer(
            TextBlockStartEvent(
                reply_id="reply-long",
                block_id="text-long",
                metadata=_metadata(sequence=1),
            ),
        )
        for index in range(chunk_count):
            observer(
                TextBlockDeltaEvent(
                    reply_id="reply-long",
                    block_id="text-long",
                    delta=chunk,
                    metadata=_metadata(sequence=index + 2),
                ),
            )
        observer(
            TextBlockEndEvent(
                reply_id="reply-long",
                block_id="text-long",
                metadata=_metadata(sequence=chunk_count + 2),
            ),
        )
        return _Result(status="success")

    app = SCRIPT.AgentTuiApp(
        run_generation=run_generation,
        run_directory=tmp_path,
        model_name="test-model",
        base_url=None,
        input_metadata=[],
        flush_callback=lambda: True,
    )

    async with app.run_test() as pilot:
        await asyncio.wait_for(app._completion_event.wait(), timeout=3)
        assert app.timeline.entries[0].content.total_chars == len(chunk) * chunk_count
        assert "完整内容见 events.jsonl" in app.timeline.entries[0].detail()
        await pilot.press("enter")

    records = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()
    ]
    assert [record["metadata"]["sequence"] for record in records] == list(
        range(1, chunk_count + 3),
    )
    deltas = [
        record["delta"]
        for record in records
        if record["type"] == "TEXT_BLOCK_DELTA"
    ]
    assert "".join(deltas) == chunk * chunk_count


@pytest.mark.asyncio
async def test_textual_navigation_fold_follow_and_artifacts(tmp_path: Path) -> None:
    flushes: list[None] = []
    events = [*_thinking_events(), *_text_events()]
    events.append(
        CustomEvent(
            name="cli_parser.generation.completed",
            value={"result": {"status": "success"}},
            metadata=_metadata(sequence=7, phase="generation", sensitive=False),
        ),
    )

    async def run_generation(observer: object) -> _Result:
        assert callable(observer)
        for event in events:
            observer(event)
            await asyncio.sleep(0)
        return _Result(status="success")

    app = SCRIPT.AgentTuiApp(
        run_generation=run_generation,
        run_directory=tmp_path,
        model_name="test-model",
        base_url="https://user:password@model.invalid/v1?api_key=secret#debug",
        input_metadata=[{"path": "sample.raw", "bytes": 10, "sha256": "0" * 64}],
        flush_callback=lambda: flushes.append(None) or True,
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.wait_for(app._completion_event.wait(), timeout=2)
        await pilot.pause(0.2)
        assert app.ready_to_exit is True
        assert app.final_exit_code == 0
        assert app.following is True
        assert app.selected_index == len(app.timeline.entries) - 1

        await pilot.press("up")
        assert app.following is False
        previous = app.selected_index
        await pilot.press("down")
        assert app.selected_index == previous + 1
        assert app.following is False
        await pilot.press("up")
        await pilot.press("end")
        assert app.following is True
        assert app.selected_index != previous

        app._select(0)
        await pilot.press("space")
        assert app.timeline.entries[0].collapsed is False
        app.timeline.entries[0].content.append(
            "\n" + "\n".join(f"detail line {index}" for index in range(200)),
        )
        app._view_dirty = True
        await app._refresh_view()
        detail_scroll = app.query_one("#detail-scroll", SCRIPT.VerticalScroll)
        before_scroll = detail_scroll.scroll_y
        await pilot.press("pagedown")
        after_page_down = detail_scroll.scroll_y
        assert after_page_down > before_scroll
        await pilot.press("pageup")
        assert detail_scroll.scroll_y < after_page_down
        await pilot.press("enter")

    records = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()
    ]
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert [record["metadata"]["sequence"] for record in records] == list(
        range(1, 8),
    )
    assert result["script_status"] == "success"
    assert result["model"]["base_url"] == "https://model.invalid/v1"
    assert result["generation_result"] == {"status": "success"}
    assert flushes == [None]


@pytest.mark.asyncio
async def test_enter_does_nothing_while_running_and_ctrl_c_cancels(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def run_generation(observer: object) -> _Result:
        del observer
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned_up.set()

    app = SCRIPT.AgentTuiApp(
        run_generation=run_generation,
        run_directory=tmp_path,
        model_name="test-model",
        base_url=None,
        input_metadata=[],
        flush_callback=lambda: True,
    )

    async with app.run_test(size=(80, 30)) as pilot:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert app.screen.has_class("narrow")
        await pilot.press("enter")
        assert app.ready_to_exit is False
        await pilot.press("ctrl+c")
        await asyncio.wait_for(cleaned_up.wait(), timeout=2)

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert app.final_exit_code == 130
    assert result["script_status"] == "cancelled"
    assert result["generation_result"] is None


@pytest.mark.asyncio
async def test_render_failure_is_reported_after_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    completed = False

    async def run_generation(observer: object) -> _Result:
        nonlocal completed
        assert callable(observer)
        observer(_thinking_events()[0])
        completed = True
        return _Result(status="success")

    def fail_render(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("private render details")

    monkeypatch.setattr(SCRIPT, "TimelineListItem", fail_render)
    app = SCRIPT.AgentTuiApp(
        run_generation=run_generation,
        run_directory=tmp_path,
        model_name="test-model",
        base_url=None,
        input_metadata=[],
        flush_callback=lambda: True,
    )

    async with app.run_test() as pilot:
        await asyncio.wait_for(app._completion_event.wait(), timeout=2)
        assert completed is True
        assert app.final_exit_code == 1
        assert app._render_error_type == "RuntimeError"
        await pilot.press("enter")

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["script_status"] == "success"
    assert result["render_error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_artifact_failure_does_not_cancel_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    completed = False

    async def run_generation(observer: object) -> _Result:
        nonlocal completed
        assert callable(observer)
        observer(_thinking_events()[0])
        completed = True
        return _Result(status="success")

    def fail_append(self: object, records: object) -> None:
        del self, records
        raise OSError("private filesystem details")

    monkeypatch.setattr(SCRIPT.EventJournal, "append", fail_append)
    app = SCRIPT.AgentTuiApp(
        run_generation=run_generation,
        run_directory=tmp_path,
        model_name="test-model",
        base_url=None,
        input_metadata=[],
        flush_callback=lambda: True,
    )

    async with app.run_test() as pilot:
        await asyncio.wait_for(app._completion_event.wait(), timeout=2)
        assert completed is True
        assert app.final_exit_code == 1
        assert app._artifact_error_type == "OSError"
        await pilot.press("enter")


@pytest.mark.asyncio
async def test_ctrl_c_during_flush_waits_for_cleanup(tmp_path: Path) -> None:
    flush_started = threading.Event()
    release_flush = threading.Event()
    flush_calls = 0

    def blocking_flush() -> bool:
        nonlocal flush_calls
        flush_calls += 1
        flush_started.set()
        return release_flush.wait(timeout=5)

    async def run_generation(observer: object) -> _Result:
        del observer
        return _Result(status="success")

    app = SCRIPT.AgentTuiApp(
        run_generation=run_generation,
        run_directory=tmp_path,
        model_name="test-model",
        base_url=None,
        input_metadata=[],
        flush_callback=blocking_flush,
    )

    async with app.run_test() as pilot:
        assert await asyncio.to_thread(flush_started.wait, 2)
        cancel = asyncio.create_task(pilot.press("ctrl+c"))
        await pilot.pause(0.1)
        assert not app._completion_event.is_set()
        assert app._generation_task is not None
        assert not app._generation_task.cancelled()
        release_flush.set()
        await asyncio.wait_for(cancel, timeout=2)

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert app.ready_to_exit is True
    assert app.final_exit_code == 130
    assert result["script_status"] == "success"
    assert flush_calls == 1


@pytest.mark.asyncio
async def test_event_serialization_failure_does_not_skip_finalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    completed = False
    calls = 0
    original_event_record = SCRIPT._event_record

    def fail_once(event: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private serialization details")
        return original_event_record(event)

    async def run_generation(observer: object) -> _Result:
        nonlocal completed
        assert callable(observer)
        for event in _text_events()[:2]:
            observer(event)
        completed = True
        return _Result(status="success")

    monkeypatch.setattr(SCRIPT, "_event_record", fail_once)
    app = SCRIPT.AgentTuiApp(
        run_generation=run_generation,
        run_directory=tmp_path,
        model_name="test-model",
        base_url=None,
        input_metadata=[],
        flush_callback=lambda: True,
    )

    async with app.run_test() as pilot:
        await asyncio.wait_for(app._completion_event.wait(), timeout=2)
        assert completed is True
        assert app.final_exit_code == 1
        assert app._artifact_error_type == "RuntimeError"
        await pilot.press("enter")

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["script_status"] == "success"
    assert result["artifact_error_type"] == "RuntimeError"
