"""Run one configured TTP generation request in a read-only Textual TUI."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from _agent_run_support import (  # noqa: E402
    ScriptConfigurationError,
    flush_laminar,
    load_command_outputs,
    new_run_directory,
    resolve_api_key,
    sanitize_base_url,
    write_json,
)
from agentscope.event import AgentEvent  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, VerticalScroll  # noqa: E402
from textual.events import Resize  # noqa: E402
from textual.widgets import Label, ListItem, ListView, Static  # noqa: E402

from cli_parser_agent import (  # noqa: E402
    GenerationPolicy,
    GenerationRequest,
    TtpGenerator,
    TtpGeneratorSettings,
)

# Configuration: edit these values, then run this file without arguments.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "deepseek-v4-pro"
BASE_URL = "https://api.deepseek.com"
COMMAND_OUTPUT_FILES = (
    PROJECT_ROOT / "testdata/real_command_outputs/ntc_templates/cisco_ios/"
    "show_interfaces_status/cisco_ios_show_interfaces_status.raw",
    PROJECT_ROOT / "testdata/real_command_outputs/ntc_templates/cisco_ios/"
    "show_interfaces_status/cisco_ios_show_interfaces_status2.raw",
    PROJECT_ROOT / "testdata/real_command_outputs/ntc_templates/cisco_ios/"
    "show_interfaces_status/cisco_ios_show_interfaces_status_pvlan.raw",
)
ARTIFACT_ROOT = PROJECT_ROOT / ".artifacts" / "agent-tui"
TOTAL_TIMEOUT_SECONDS = 1_800.0
MAX_AGENT_ROUNDS = 24
MAX_TTP_SUBMISSIONS = 16
MAX_SCHEMA_NO_TOOL_RETRIES = 3
MAX_TTP_NO_TOOL_RETRIES = 3
TTP_VALIDATION_TIMEOUT_SECONDS = 20.0
STREAM = True
TEMPERATURE = 0.0
PARALLEL_TOOL_CALLS = False
MAX_TOKENS = 8_192
CONTEXT_SIZE = 128_000
MODEL_MAX_RETRIES = 2
MODEL_TIMEOUT_SECONDS = 120.0

SCRIPT_VERSION = 1
MAX_TIMELINE_CONTENT_CHARS = 64 * 1024
TIMELINE_HEAD_CHARS = 48 * 1024
TIMELINE_TAIL_CHARS = MAX_TIMELINE_CONTENT_CHARS - TIMELINE_HEAD_CHARS
NARROW_TERMINAL_WIDTH = 100
EVENT_BATCH_MAX_ITEMS = 256
EVENT_BATCH_WINDOW_SECONDS = 0.05
EventObserver = Callable[[AgentEvent], None]
GenerationRunner = Callable[[EventObserver], Awaitable[Any]]
_REDACTED = "[REDACTED]"
_CREDENTIAL_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "lmnr_project_api_key",
    "openai_api_key",
    "project_api_key",
    "proxy_authorization",
}
_STOP = object()


def _resolve_api_key() -> str:
    return resolve_api_key()


def _load_command_outputs(
    paths: tuple[Path, ...] = COMMAND_OUTPUT_FILES,
) -> tuple[list[str], list[dict[str, Any]]]:
    return load_command_outputs(paths, project_root=PROJECT_ROOT)


def _new_run_directory() -> Path:
    return new_run_directory(ARTIFACT_ROOT)


def _write_json(path: Path, value: Any) -> None:
    write_json(path, value)


def _flush_laminar() -> bool:
    return flush_laminar()


def _is_interactive_terminal(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool:
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    return bool(input_stream.isatty() and output_stream.isatty())


def _json_safe(value: Any) -> Any:
    """Convert event data to JSON without serializing opaque objects or secrets."""

    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            shown_key = str(key)
            normalized = shown_key.strip().lower().replace("-", "_")
            is_credential = (
                normalized in _CREDENTIAL_KEYS
                or normalized.endswith("_api_key")
            )
            output[shown_key] = (
                _REDACTED if is_credential else _json_safe(item)
            )
        return output
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"unserialized_type": type(value).__name__}


def _event_record(event: AgentEvent | Any) -> dict[str, Any]:
    if isinstance(event, BaseModel):
        payload = event.model_dump(mode="json")
    elif isinstance(event, Mapping):
        payload = dict(event)
    else:
        payload = {"unserialized_type": type(event).__name__}
    safe_payload = _json_safe(payload)
    assert isinstance(safe_payload, dict)
    return {"event_class": type(event).__name__, **safe_payload}


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(slots=True)
class BoundedContent:
    """Keep a bounded head/tail rendering while counting omitted characters."""

    head: str = ""
    tail: str = ""
    total_chars: int = 0

    def append(self, value: str) -> None:
        if not value:
            return
        self.total_chars += len(value)
        remaining_head = max(0, TIMELINE_HEAD_CHARS - len(self.head))
        if remaining_head:
            self.head += value[:remaining_head]
            value = value[remaining_head:]
        if value:
            self.tail = (self.tail + value)[-TIMELINE_TAIL_CHARS:]

    @classmethod
    def from_value(cls, value: str) -> BoundedContent:
        content = cls()
        content.append(value)
        return content

    def render(self) -> str:
        omitted = self.total_chars - len(self.head) - len(self.tail)
        if omitted <= 0:
            return self.head + self.tail
        marker = f"\n\n… 已省略 {omitted} 个字符；完整内容见 events.jsonl …\n\n"
        return self.head + marker + self.tail


@dataclass(slots=True)
class TimelineEntry:
    key: str
    kind: str
    title: str
    phase: str
    elapsed_seconds: float
    sensitive: bool
    content: BoundedContent = field(default_factory=BoundedContent)
    collapsed: bool = False
    manually_toggled: bool = False
    complete: bool = False

    def detail(self) -> str:
        if self.kind == "thinking" and self.collapsed:
            return "思考块已折叠。"
        return self.content.render() or "（暂无内容）"

    def list_label(self) -> Text:
        marker = "  "
        if self.kind == "thinking":
            marker = "+ " if self.collapsed else "− "
        phase = f"[{self.phase}] " if self.phase else ""
        title = self.title.replace("\n", " ")[:96]
        return Text(f"{marker}{phase}{title}", style="dim" if self.complete else "bold")


@dataclass(slots=True)
class TimelineUpdate:
    created: list[int] = field(default_factory=list)
    changed: set[int] = field(default_factory=set)


class TimelineModel:
    """Reduce raw AgentScope and project events into navigable blocks."""

    def __init__(self) -> None:
        self.entries: list[TimelineEntry] = []
        self._by_key: dict[str, int] = {}
        self._active_thinking: int | None = None

    @staticmethod
    def _metadata(record: Mapping[str, Any]) -> tuple[str, float, bool]:
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            return "", 0.0, False
        elapsed = metadata.get("elapsed_seconds", 0.0)
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool):
            elapsed = 0.0
        return (
            str(metadata.get("phase", "")),
            float(elapsed),
            metadata.get("sensitive") is True,
        )

    def _append(
        self,
        update: TimelineUpdate,
        *,
        key: str,
        kind: str,
        title: str,
        record: Mapping[str, Any],
        content: str = "",
        complete: bool = False,
    ) -> int:
        phase, elapsed, sensitive = self._metadata(record)
        entry = TimelineEntry(
            key=key,
            kind=kind,
            title=title,
            phase=phase,
            elapsed_seconds=elapsed,
            sensitive=sensitive,
            content=BoundedContent.from_value(content),
            complete=complete,
        )
        index = len(self.entries)
        self.entries.append(entry)
        self._by_key[key] = index
        update.created.append(index)
        update.changed.add(index)
        return index

    def _close_active_thinking(self, update: TimelineUpdate) -> None:
        if self._active_thinking is None:
            return
        entry = self.entries[self._active_thinking]
        entry.complete = True
        if not entry.manually_toggled:
            entry.collapsed = True
        update.changed.add(self._active_thinking)
        self._active_thinking = None

    def _stream_start(
        self,
        update: TimelineUpdate,
        record: Mapping[str, Any],
        *,
        kind: str,
        title: str,
        id_field: str,
        prefix: str,
    ) -> None:
        identifier = str(record.get(id_field) or record.get("id") or len(self.entries))
        index = self._append(
            update,
            key=f"{prefix}:{identifier}",
            kind=kind,
            title=title,
            record=record,
        )
        if kind == "thinking":
            self._active_thinking = index

    def _stream_delta(
        self,
        update: TimelineUpdate,
        record: Mapping[str, Any],
        *,
        id_field: str,
        prefix: str,
    ) -> None:
        index = self._by_key.get(f"{prefix}:{record.get(id_field, '')}")
        if index is None:
            return
        delta = record.get("delta", "")
        self.entries[index].content.append(
            delta if isinstance(delta, str) else _pretty_json(delta),
        )
        update.changed.add(index)

    def _stream_end(
        self,
        update: TimelineUpdate,
        record: Mapping[str, Any],
        *,
        id_field: str,
        prefix: str,
    ) -> None:
        index = self._by_key.get(f"{prefix}:{record.get(id_field, '')}")
        if index is None:
            return
        entry = self.entries[index]
        entry.complete = True
        if entry.kind == "thinking" and not entry.manually_toggled:
            entry.collapsed = True
        if self._active_thinking == index:
            self._active_thinking = None
        update.changed.add(index)

    def _append_custom_parts(
        self,
        update: TimelineUpdate,
        record: Mapping[str, Any],
    ) -> None:
        name = str(record.get("name", "custom"))
        value = record.get("value", {})
        value_mapping = value if isinstance(value, Mapping) else {}
        event_id = str(record.get("id") or len(self.entries))
        titles = {
            "cli_parser.generation.started": "生成开始",
            "cli_parser.generation.completed": "生成完成",
            "cli_parser.generation.cancelled": "生成已取消",
            "cli_parser.generation.exception": "生成异常",
            "cli_parser.phase.started": "阶段开始",
            "cli_parser.phase.input_prepared": "阶段输入",
            "cli_parser.phase.sampling_completed": "输入采样完成",
            "cli_parser.phase.completed": "阶段完成",
            "cli_parser.model.context_snapshot": "模型上下文",
            "cli_parser.model.output_discarded": "模型输出已丢弃",
            "cli_parser.no_tool.retry": "无工具调用重试",
            "cli_parser.tool.result": "结构化工具反馈",
            "cli_parser.final_validation.started": "Agent 外全文验收开始",
            "cli_parser.final_validation.completed": "Agent 外全文验收完成",
        }
        tool_name = str(value_mapping.get("tool_name", ""))
        title = titles.get(name, name)
        if tool_name:
            title = f"{title} · {tool_name}"
        self._append(
            update,
            key=f"custom:{event_id}",
            kind="custom",
            title=title,
            record=record,
            content=_pretty_json(value),
            complete=True,
        )

        if name != "cli_parser.tool.result":
            return
        tool_input = value_mapping.get("input")
        tool_output = value_mapping.get("output")
        if isinstance(tool_input, Mapping):
            if "result_schema" in tool_input:
                self._append(
                    update,
                    key=f"schema:{event_id}",
                    kind="schema",
                    title="JSON Schema 候选",
                    record=record,
                    content=_pretty_json(tool_input["result_schema"]),
                    complete=True,
                )
            if "ttp_template" in tool_input:
                self._append(
                    update,
                    key=f"ttp:{event_id}",
                    kind="ttp",
                    title="TTP 模板候选",
                    record=record,
                    content=str(tool_input["ttp_template"]),
                    complete=True,
                )
        if not isinstance(tool_output, Mapping):
            return
        if "capture" in tool_output:
            self._append(
                update,
                key=f"capture:{event_id}",
                kind="capture",
                title="Capture",
                record=record,
                content=_pretty_json(tool_output["capture"]),
                complete=True,
            )
        issues = tool_output.get("issues")
        if (
            isinstance(issues, Sequence)
            and not isinstance(issues, (str, bytes))
            and issues
        ):
            self._append(
                update,
                key=f"issues:{event_id}",
                kind="issues",
                title="Issues",
                record=record,
                content=_pretty_json(issues),
                complete=True,
            )

    def apply(self, record: Mapping[str, Any]) -> TimelineUpdate:
        """Apply one JSON event record and return its visible changes."""

        update = TimelineUpdate()
        event_type = str(record.get("type", ""))
        if event_type not in {
            "THINKING_BLOCK_DELTA",
            "THINKING_BLOCK_END",
        }:
            self._close_active_thinking(update)

        starts = {
            "THINKING_BLOCK_START": (
                "thinking",
                "模型思考",
                "block_id",
                "thinking",
            ),
            "TEXT_BLOCK_START": ("text", "模型回复", "block_id", "text"),
            "TOOL_CALL_START": (
                "tool_call",
                f"工具调用 · {record.get('tool_call_name', 'unknown')}",
                "tool_call_id",
                "tool-call",
            ),
            "TOOL_RESULT_START": (
                "tool_result",
                f"工具结果 · {record.get('tool_call_name', 'unknown')}",
                "tool_call_id",
                "tool-result",
            ),
        }
        deltas = {
            "THINKING_BLOCK_DELTA": ("block_id", "thinking"),
            "TEXT_BLOCK_DELTA": ("block_id", "text"),
            "TOOL_CALL_DELTA": ("tool_call_id", "tool-call"),
            "TOOL_RESULT_TEXT_DELTA": ("tool_call_id", "tool-result"),
        }
        ends = {
            "THINKING_BLOCK_END": ("block_id", "thinking"),
            "TEXT_BLOCK_END": ("block_id", "text"),
            "TOOL_CALL_END": ("tool_call_id", "tool-call"),
            "TOOL_RESULT_END": ("tool_call_id", "tool-result"),
        }
        if event_type in starts:
            kind, title, id_field, prefix = starts[event_type]
            self._stream_start(
                update,
                record,
                kind=kind,
                title=title,
                id_field=id_field,
                prefix=prefix,
            )
        elif event_type in deltas:
            id_field, prefix = deltas[event_type]
            self._stream_delta(
                update,
                record,
                id_field=id_field,
                prefix=prefix,
            )
        elif event_type in ends:
            id_field, prefix = ends[event_type]
            self._stream_end(
                update,
                record,
                id_field=id_field,
                prefix=prefix,
            )
        elif event_type == "CUSTOM":
            self._append_custom_parts(update, record)
        else:
            title = event_type.replace("_", " ").title() or "Agent 事件"
            content = {
                key: value
                for key, value in record.items()
                if key not in {"event_class", "metadata", "type"}
            }
            self._append(
                update,
                key=f"event:{record.get('id', len(self.entries))}",
                kind="event",
                title=title,
                record=record,
                content=_pretty_json(content),
                complete=True,
            )
        return update

    def toggle_thinking(self, index: int) -> bool:
        if not 0 <= index < len(self.entries):
            return False
        entry = self.entries[index]
        if entry.kind != "thinking":
            return False
        entry.collapsed = not entry.collapsed
        entry.manually_toggled = True
        return True


@dataclass(slots=True)
class TuiStatus:
    model_name: str
    phase: str = "准备"
    model_rounds: int = 0
    schema_submissions: int = 0
    ttp_submissions: int = 0
    candidate_available: bool = False
    state: str = "运行中"
    termination_reason: str | None = None
    elapsed_seconds: float | None = None

    def observe(self, record: Mapping[str, Any]) -> None:
        metadata = record.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("phase"):
            self.phase = str(metadata["phase"])
        event_type = str(record.get("type", ""))
        if event_type == "MODEL_CALL_START":
            self.model_rounds += 1
        elif event_type == "CUSTOM":
            name = record.get("name")
            value = record.get("value")
            value_mapping = value if isinstance(value, Mapping) else {}
            if name == "cli_parser.tool.result":
                output = value_mapping.get("output")
                if isinstance(output, Mapping):
                    self.candidate_available = (
                        output.get("validated_candidate_available") is True
                    )
                    schema_submissions = output.get("schema_submission")
                    if isinstance(schema_submissions, int) and not isinstance(
                        schema_submissions,
                        bool,
                    ):
                        self.schema_submissions = schema_submissions
                    ttp_submissions = output.get("ttp_submission")
                    if isinstance(ttp_submissions, int) and not isinstance(
                        ttp_submissions,
                        bool,
                    ):
                        self.ttp_submissions = ttp_submissions
            elif name == "cli_parser.generation.completed":
                result = value_mapping.get("result")
                self.state = (
                    str(result.get("status", "完成"))
                    if isinstance(result, Mapping)
                    else "完成"
                )
                result_metadata = (
                    result.get("metadata") if isinstance(result, Mapping) else None
                )
                if isinstance(result_metadata, Mapping):
                    termination_reason = result_metadata.get("termination_reason")
                    if termination_reason:
                        self.termination_reason = str(termination_reason)
                    result_elapsed = result_metadata.get("elapsed_seconds")
                    if isinstance(result_elapsed, (int, float)) and not isinstance(
                        result_elapsed,
                        bool,
                    ):
                        self.elapsed_seconds = float(result_elapsed)
                    schema_submissions = result_metadata.get("schema_submissions")
                    ttp_submissions = result_metadata.get("ttp_submissions")
                    if isinstance(schema_submissions, int) and not isinstance(
                        schema_submissions,
                        bool,
                    ):
                        self.schema_submissions = schema_submissions
                    if isinstance(ttp_submissions, int) and not isinstance(
                        ttp_submissions,
                        bool,
                    ):
                        self.ttp_submissions = ttp_submissions
            elif name == "cli_parser.generation.cancelled":
                self.state = "已取消"
                self.termination_reason = "cancelled"
            elif name == "cli_parser.generation.exception":
                self.state = "异常"
                self.termination_reason = "exception"

            if name in {
                "cli_parser.generation.completed",
                "cli_parser.generation.cancelled",
                "cli_parser.generation.exception",
            } and isinstance(metadata, Mapping):
                event_elapsed = metadata.get("elapsed_seconds")
                if isinstance(event_elapsed, (int, float)) and not isinstance(
                    event_elapsed,
                    bool,
                ):
                    self.elapsed_seconds = float(event_elapsed)


class QueueEventObserver:
    """A synchronous observer whose only normal action is ``put_nowait``."""

    def __init__(self, queue: asyncio.Queue[AgentEvent | object]) -> None:
        self._queue = queue
        self.failed = False

    def __call__(self, event: AgentEvent) -> None:
        if self.failed:
            return
        try:
            self._queue.put_nowait(event)
        except BaseException:
            self.failed = True


class EventJournal:
    """Append complete observer events to an LF-delimited JSON transcript."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: TextIO | None = None

    def open(self) -> None:
        self._stream = self.path.open("w", encoding="utf-8", newline="\n")

    def append(self, records: Sequence[Mapping[str, Any]]) -> None:
        if self._stream is None:
            raise RuntimeError("event journal is not open")
        lines = [
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in records
        ]
        if lines:
            self._stream.write("\n".join(lines) + "\n")
            self._stream.flush()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None


class TimelineListItem(ListItem):
    def __init__(self, entry_index: int, label: Text) -> None:
        super().__init__(Label(label))
        self.entry_index = entry_index


class AgentTuiApp(App[int]):
    """Read-only terminal observer for one generation request."""

    CSS = """
    Screen { layout: vertical; }
    #status {
        height: 3;
        padding: 0 1;
        border-bottom: solid $accent;
    }
    #body { height: 1fr; layout: horizontal; }
    #timeline {
        width: 38%;
        min-width: 32;
        border-right: solid $accent;
    }
    #detail-scroll { width: 1fr; padding: 0 1; }
    #detail { width: 100%; }
    .narrow #body { layout: vertical; }
    .narrow #timeline {
        width: 100%;
        height: 42%;
        min-width: 0;
        border-right: none;
        border-bottom: solid $accent;
    }
    .narrow #detail-scroll { width: 100%; height: 1fr; }
    """

    BINDINGS = [
        Binding("up", "previous_event", show=False, priority=True),
        Binding("down", "next_event", show=False, priority=True),
        Binding("space", "toggle_thinking", show=False, priority=True),
        Binding("end", "follow_latest", show=False, priority=True),
        Binding("pageup", "detail_page_up", show=False, priority=True),
        Binding("pagedown", "detail_page_down", show=False, priority=True),
        Binding("enter", "exit_when_complete", show=False, priority=True),
        Binding("ctrl+c", "cancel_generation", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        run_generation: GenerationRunner,
        run_directory: Path,
        model_name: str,
        base_url: str | None,
        input_metadata: Sequence[Mapping[str, Any]],
        flush_callback: Callable[[], bool] = _flush_laminar,
    ) -> None:
        super().__init__()
        self.run_generation = run_generation
        self.run_directory = run_directory
        self.events_path = run_directory / "events.jsonl"
        self.result_path = run_directory / "result.json"
        self.model_name = model_name
        self.base_url = base_url
        self.input_metadata = [dict(item) for item in input_metadata]
        self.flush_callback = flush_callback
        self.timeline = TimelineModel()
        self.status_model = TuiStatus(model_name=model_name)
        self.event_queue: asyncio.Queue[AgentEvent | object] = asyncio.Queue()
        self.observer = QueueEventObserver(self.event_queue)
        self.journal = EventJournal(self.events_path)
        self.started_at = datetime.now(UTC)
        self.started_monotonic = time.monotonic()
        self.selected_index: int | None = None
        self.following = True
        self.ready_to_exit = False
        self.final_exit_code = 1
        self.cancel_requested = False
        self._dirty_indices: set[int] = set()
        self._rendered_entry_count = 0
        self._view_dirty = True
        self._artifact_error_type: str | None = None
        self._render_error_type: str | None = None
        self._generation_task: asyncio.Task[None] | None = None
        self._agent_task: asyncio.Task[Any] | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._completion_event = asyncio.Event()
        self.laminar_flush_attempted = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="body"):
            yield ListView(id="timeline")
            with VerticalScroll(id="detail-scroll"):
                yield Static("等待事件…", id="detail")

    async def on_mount(self) -> None:
        self.screen.set_class(self.size.width < NARROW_TERMINAL_WIDTH, "narrow")
        try:
            await asyncio.to_thread(self.journal.open)
        except Exception as error:
            self._artifact_error_type = type(error).__name__
        self._consumer_task = asyncio.create_task(self._consume_events())
        self._generation_task = asyncio.create_task(self._execute_generation())
        self.set_interval(0.1, self._refresh_view)
        await self._refresh_view()

    def on_resize(self, event: Resize) -> None:
        self.screen.set_class(
            event.size.width < NARROW_TERMINAL_WIDTH,
            "narrow",
        )

    def _record_artifact_error(self, error: BaseException) -> None:
        if self._artifact_error_type is None:
            self._artifact_error_type = type(error).__name__

    async def _close_journal(self) -> None:
        try:
            await asyncio.to_thread(self.journal.close)
        except Exception as error:
            self._record_artifact_error(error)

    async def _consume_events(self) -> None:
        try:
            stopped = False
            while not stopped:
                item = await self.event_queue.get()
                if item is _STOP:
                    break
                batch = [item]
                batch_deadline = (
                    asyncio.get_running_loop().time()
                    + EVENT_BATCH_WINDOW_SECONDS
                )
                while len(batch) < EVENT_BATCH_MAX_ITEMS:
                    remaining = batch_deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        queued = await asyncio.wait_for(
                            self.event_queue.get(),
                            timeout=remaining,
                        )
                    except TimeoutError:
                        break
                    if queued is _STOP:
                        stopped = True
                        break
                    batch.append(queued)

                records: list[dict[str, Any]] = []
                for event in batch:
                    try:
                        records.append(_event_record(event))
                    except Exception as error:
                        self._record_artifact_error(error)

                if self._artifact_error_type is None and records:
                    try:
                        await asyncio.to_thread(self.journal.append, records)
                    except Exception as error:
                        self._record_artifact_error(error)
                        await self._close_journal()

                for record in records:
                    self.status_model.observe(record)
                    try:
                        update = self.timeline.apply(record)
                    except Exception as error:
                        self._render_error_type = type(error).__name__
                        continue
                    self._dirty_indices.update(update.changed)
                    if update.created and self.following:
                        self.selected_index = update.created[-1]
                    self._view_dirty = True
        finally:
            await self._close_journal()

    async def _execute_generation(self) -> None:
        generation_result: Any | None = None
        script_status = "failed"
        exception_type: str | None = None
        try:
            self._agent_task = asyncio.create_task(
                self.run_generation(self.observer),
            )
            if self.cancel_requested:
                self._agent_task.cancel()
            generation_result = await self._agent_task
            result_status = getattr(generation_result, "status", "failed")
            script_status = "success" if result_status == "success" else "failed"
            self.status_model.state = str(result_status)
        except asyncio.CancelledError as error:
            script_status = "cancelled" if self.cancel_requested else "exception"
            self.status_model.state = "已取消" if self.cancel_requested else "异常"
            exception_type = type(error).__name__
        except BaseException as error:
            script_status = "exception"
            exception_type = type(error).__name__
            self.status_model.state = "异常"
        finally:
            self._agent_task = None
            self.event_queue.put_nowait(_STOP)
            if self._consumer_task is not None:
                try:
                    await self._consumer_task
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    self._record_artifact_error(error)
            await self._refresh_view()
            await self._finalize_run(
                script_status=script_status,
                exception_type=exception_type,
                generation_result=generation_result,
            )

    async def _finalize_run(
        self,
        *,
        script_status: str,
        exception_type: str | None,
        generation_result: Any | None,
    ) -> None:
        result_payload = {
            "script_version": SCRIPT_VERSION,
            "script_status": script_status,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "model": {
                "name": self.model_name,
                "base_url": sanitize_base_url(self.base_url),
            },
            "input_files": self.input_metadata,
            "events_jsonl": str(self.events_path),
            "artifact_error_type": self._artifact_error_type,
            "render_error_type": self._render_error_type,
            "exception_type": exception_type,
            "generation_result": (
                generation_result.model_dump(mode="json")
                if isinstance(generation_result, BaseModel)
                else _json_safe(generation_result)
                if generation_result is not None
                else None
            ),
        }
        try:
            _write_json(self.result_path, result_payload)
        except Exception as error:
            self._record_artifact_error(error)
        self.laminar_flush_attempted = True
        try:
            await asyncio.to_thread(self.flush_callback)
        except Exception as error:
            print(
                f"warning: Laminar flush failed ({type(error).__name__})",
                file=sys.stderr,
            )
        if script_status == "cancelled":
            self.final_exit_code = 130
        elif (
            script_status == "success"
            and self._artifact_error_type is None
            and self._render_error_type is None
            and not self.observer.failed
        ):
            self.final_exit_code = 0
        else:
            self.final_exit_code = 1
        self.ready_to_exit = True
        self._completion_event.set()

    def _status_text(self) -> Text:
        elapsed = (
            self.status_model.elapsed_seconds
            if self.status_model.elapsed_seconds is not None
            else time.monotonic() - self.started_monotonic
        )
        candidate = "有" if self.status_model.candidate_available else "无"
        state = self.status_model.state
        if self.status_model.termination_reason:
            state = f"{state}/{self.status_model.termination_reason}"
        return Text(
            "  ".join(
                (
                    f"模型 {self.status_model.model_name}",
                    f"阶段 {self.status_model.phase}",
                    f"耗时 {elapsed:.1f}s",
                    f"轮次 {self.status_model.model_rounds}",
                    f"Schema {self.status_model.schema_submissions}",
                    f"TTP {self.status_model.ttp_submissions}",
                    f"有效候选 {candidate}",
                    f"状态 {state}",
                ),
            ),
        )

    async def _refresh_view(self) -> None:
        try:
            self.query_one("#status", Static).update(self._status_text())
            if not self._view_dirty:
                return
            list_view = self.query_one("#timeline", ListView)
            if self._rendered_entry_count < len(self.timeline.entries):
                new_items = [
                    TimelineListItem(index, self.timeline.entries[index].list_label())
                    for index in range(
                        self._rendered_entry_count,
                        len(self.timeline.entries),
                    )
                ]
                await list_view.extend(new_items)
                self._rendered_entry_count = len(self.timeline.entries)
            for index in sorted(self._dirty_indices):
                if index >= len(list_view.children):
                    continue
                item = list_view.children[index]
                if isinstance(item, TimelineListItem):
                    item.query_one(Label).update(
                        self.timeline.entries[index].list_label(),
                    )
            self._dirty_indices.clear()

            if self.following and self.timeline.entries:
                self.selected_index = len(self.timeline.entries) - 1
            if self.selected_index is not None and self.timeline.entries:
                self.selected_index = min(
                    self.selected_index,
                    len(self.timeline.entries) - 1,
                )
                list_view.index = self.selected_index
                entry = self.timeline.entries[self.selected_index]
                detail = Text()
                detail.append(f"{entry.title}\n", style="bold")
                detail.append(
                    f"阶段: {entry.phase or '-'}  "
                    f"时间: {entry.elapsed_seconds:.3f}s\n\n",
                    style="dim",
                )
                detail.append(entry.detail())
                self.query_one("#detail", Static).update(detail)
            self._view_dirty = False
        except Exception as error:
            self._render_error_type = type(error).__name__

    def _select(self, index: int, *, follow: bool = False) -> None:
        if not self.timeline.entries:
            return
        self.selected_index = max(0, min(index, len(self.timeline.entries) - 1))
        self.following = follow
        self._view_dirty = True

    def action_previous_event(self) -> None:
        self._select((self.selected_index or 0) - 1)

    def action_next_event(self) -> None:
        current = self.selected_index if self.selected_index is not None else -1
        self._select(current + 1)

    def action_toggle_thinking(self) -> None:
        if self.selected_index is None:
            return
        if self.timeline.toggle_thinking(self.selected_index):
            self._dirty_indices.add(self.selected_index)
            self._view_dirty = True

    def action_follow_latest(self) -> None:
        if self.timeline.entries:
            self._select(len(self.timeline.entries) - 1, follow=True)

    def action_detail_page_up(self) -> None:
        self.query_one("#detail-scroll", VerticalScroll).scroll_page_up(animate=False)

    def action_detail_page_down(self) -> None:
        self.query_one("#detail-scroll", VerticalScroll).scroll_page_down(animate=False)

    def action_exit_when_complete(self) -> None:
        if self.ready_to_exit:
            self.exit(self.final_exit_code, return_code=self.final_exit_code)

    async def action_cancel_generation(self) -> None:
        if self.ready_to_exit:
            self.exit(self.final_exit_code, return_code=self.final_exit_code)
            return
        self.cancel_requested = True
        self.status_model.state = "正在取消"
        if self._agent_task is not None and not self._agent_task.done():
            self._agent_task.cancel()
        await self._completion_event.wait()
        self.final_exit_code = 130
        self.exit(130, return_code=130)


async def _run() -> int:
    command_outputs, input_metadata = _load_command_outputs()
    api_key = _resolve_api_key()
    app: AgentTuiApp | None = None
    try:
        settings = TtpGeneratorSettings(
            api_key=api_key,
            model_name=MODEL_NAME,
            base_url=BASE_URL,
            stream=STREAM,
            temperature=TEMPERATURE,
            parallel_tool_calls=PARALLEL_TOOL_CALLS,
            max_tokens=MAX_TOKENS,
            context_size=CONTEXT_SIZE,
            model_max_retries=MODEL_MAX_RETRIES,
            model_timeout_seconds=MODEL_TIMEOUT_SECONDS,
        )
        policy = GenerationPolicy(
            total_timeout_seconds=TOTAL_TIMEOUT_SECONDS,
            max_agent_rounds=MAX_AGENT_ROUNDS,
            max_ttp_submissions=MAX_TTP_SUBMISSIONS,
            max_schema_no_tool_retries=MAX_SCHEMA_NO_TOOL_RETRIES,
            max_ttp_no_tool_retries=MAX_TTP_NO_TOOL_RETRIES,
            ttp_validation_timeout_seconds=TTP_VALIDATION_TIMEOUT_SECONDS,
        )
        request = GenerationRequest(command_outputs=command_outputs)
        generator = TtpGenerator(settings=settings, policy=policy)
        try:
            run_directory = _new_run_directory()
        except Exception as error:
            print(
                "artifact error: unable to create run directory "
                f"({type(error).__name__})",
                file=sys.stderr,
            )
            return 1

        async def run_generation(observer: EventObserver) -> Any:
            return await generator.generate(request, observer=observer)

        app = AgentTuiApp(
            run_generation=run_generation,
            run_directory=run_directory,
            model_name=MODEL_NAME,
            base_url=BASE_URL,
            input_metadata=input_metadata,
        )
        result = await app.run_async()
        return int(result if result is not None else app.final_exit_code)
    finally:
        if app is None or not app.laminar_flush_attempted:
            _flush_laminar()


def main() -> int:
    if not _is_interactive_terminal():
        print(
            "configuration error: run_agent_tui.py requires an interactive terminal.",
            file=sys.stderr,
        )
        return 2
    try:
        return asyncio.run(_run())
    except ScriptConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
