"""Safe static inspection and isolated execution of generated TTP templates."""

from __future__ import annotations

import ast
import contextlib
import ipaddress
import json
import logging
import math
import multiprocessing
import os
import re
import sys
import tempfile
import time
import tokenize
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import StringIO
from typing import Any

from ..contracts import ValidationIssue
from .json_schema import (
    MAX_SCHEMA_BYTES,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_PROPERTIES,
    validate_records_against_schema,
)

MAX_TTP_TEMPLATE_BYTES = 64 * 1024
MAX_TTP_GROUP_DEPTH = 16
MAX_TTP_REGEX_CHARS = 2_048
MAX_TTP_ARGUMENT_CHARS = 4_096
MAX_TTP_ATTRIBUTE_CHARS = 64 * 1024
MAX_TTP_RESULT_BYTES = 8 * 1024 * 1024
MAX_TTP_VALIDATION_ISSUES = 100
DEFAULT_TTP_TIMEOUT_SECONDS = 20.0

_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_GROUP_SEGMENT_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\*)?$",
)
_XML_TAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_VARIABLE_RE = re.compile(r"{{([\s\S]*?)}}")
_NUMBER_TOKEN_RE = re.compile(
    r"(?<![\w.])[-+]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])",
)
_DANGEROUS_XML_RE = re.compile(
    r"<!\s*(?:DOCTYPE|ENTITY)|<\?xml-stylesheet|<\?",
    re.IGNORECASE,
)

_BUILTIN_PATTERNS = {
    "DIGIT",
    "IP",
    "IPV6",
    "MAC",
    "ORPHRASE",
    "PHRASE",
    "PREFIX",
    "PREFIXV6",
    "ROW",
    "WORD",
}
_NO_ARGUMENT_ATTRIBUTES = _BUILTIN_PATTERNS | {
    "_end_",
    "_exact_",
    "_exact_space_",
    "_headers_",
    "_line_",
    "_start_",
    "columns",
    "isdigit",
    "notdigit",
    "to_cidr",
    "to_float",
    "to_int",
    "to_str",
}
_STRING_CONDITIONS = {
    "contains",
    "equal",
    "exclude",
    "notequal",
}
_REGEX_CONDITIONS = {
    "contains_re",
    "endswith_re",
    "exclude_re",
    "notendswith_re",
    "notstartswith_re",
    "startswith_re",
}
_ALLOWED_ATTRIBUTES = (
    _NO_ARGUMENT_ATTRIBUTES
    | _STRING_CONDITIONS
    | _REGEX_CONDITIONS
    | {"is_ip", "item", "joinmatches", "re", "to_ip", "to_net"}
)
_GROUP_FILTERS = {"contains", "containsall", "equal", "exclude", "excludeall"}


@dataclass(frozen=True, slots=True)
class TtpValidationResult:
    """Captured records and issues produced by one full validation run.

    Once the isolated parser returns an index-mapped result, ``records`` keeps
    that result even when a record is empty or later acceptance checks fail.
    """

    records: list[dict[str, Any]]
    issues: list[ValidationIssue]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


@dataclass(frozen=True, slots=True)
class _ParsedAttribute:
    name: str
    args: tuple[str | int | float | bool | None, ...]
    kwargs: Mapping[str, str | int | float | bool | None]


@dataclass(frozen=True, slots=True)
class _ParsedVariableHead:
    name: str
    is_ignore: bool = False


class _InvalidIgnoreSyntax(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _TtpLimits:
    template_bytes: int
    group_depth: int
    regex_chars: int
    argument_chars: int

    @property
    def attribute_chars(self) -> int:
        return min(
            MAX_TTP_ATTRIBUTE_CHARS,
            self.argument_chars * 16 + 512,
        )


def _issue(
    code: str,
    message: str,
    *,
    path: str | None = None,
    output_index: int | None = None,
    stage: str = "ttp",
    details: dict[str, Any] | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        stage=stage,
        message=message,
        path=path[:2_048] if path is not None else None,
        output_index=output_index,
        details=details or {},
    )


def _effective_limit(value: int, hard_limit: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, hard_limit)


def _safe_argument(
    node: ast.expr,
    limits: _TtpLimits,
) -> str | int | float | bool | None:
    if isinstance(node, ast.Constant) and isinstance(
        node.value,
        (str, int, float, bool, type(None)),
    ):
        if isinstance(node.value, float) and not (
            float("-inf") < node.value < float("inf")
        ):
            raise ValueError("non-finite numbers are not allowed")
        if isinstance(node.value, str) and len(node.value) > limits.argument_chars:
            raise ValueError("string argument is too long")
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) is int
    ):
        return -node.operand.value
    # TTP intentionally treats unresolved bare names as string arguments.
    if isinstance(node, ast.Name) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", node.id):
        if len(node.id) > limits.argument_chars:
            raise ValueError("name argument is too long")
        return node.id
    raise ValueError("only literal values and bare names are allowed as arguments")


def _parse_attribute(text: str, limits: _TtpLimits) -> _ParsedAttribute:
    if len(text) > limits.attribute_chars:
        raise ValueError("attribute expression is too long")
    try:
        expression = ast.parse(text.strip(), mode="eval").body
    except (SyntaxError, ValueError) as exc:
        raise ValueError("attribute expression is not valid") from exc

    if isinstance(expression, ast.Name):
        return _ParsedAttribute(expression.id, (), {})
    if not isinstance(expression, ast.Call) or not isinstance(
        expression.func,
        ast.Name,
    ):
        raise ValueError("attribute must be a simple function name or call")
    if any(isinstance(argument, ast.Starred) for argument in expression.args):
        raise ValueError("starred arguments are not allowed")
    if len(expression.args) + len(expression.keywords) > 16:
        raise ValueError("too many attribute arguments")
    args = tuple(_safe_argument(argument, limits) for argument in expression.args)
    kwargs: dict[str, str | int | float | bool | None] = {}
    for keyword in expression.keywords:
        if keyword.arg is None or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*",
            keyword.arg,
        ):
            raise ValueError("expanded or invalid keyword arguments are not allowed")
        kwargs[keyword.arg] = _safe_argument(keyword.value, limits)
    return _ParsedAttribute(expression.func.id, args, kwargs)


def _validate_regex(pattern: Any, limits: _TtpLimits) -> None:
    if not isinstance(pattern, str):
        raise ValueError("regular expression argument must be a string")
    if len(pattern) > limits.regex_chars:
        raise ValueError("regular expression argument is too long")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError("regular expression argument is invalid") from exc


def _parse_variable_head(text: str, limits: _TtpLimits) -> _ParsedVariableHead:
    """Parse a result variable or TTP's special ignore capture."""

    mentions_ignore = bool(
        re.search(r"(?<![A-Za-z0-9_])ignore(?![A-Za-z0-9_])", text),
    )
    if len(text) > limits.attribute_chars:
        if mentions_ignore:
            raise _InvalidIgnoreSyntax
        raise ValueError("variable expression is too long")
    try:
        expression = ast.parse(text.strip(), mode="eval").body
    except (SyntaxError, ValueError) as exc:
        if mentions_ignore:
            raise _InvalidIgnoreSyntax from exc
        raise ValueError("variable expression is not valid") from exc

    if isinstance(expression, ast.Name):
        return _ParsedVariableHead(
            name=expression.id,
            is_ignore=expression.id == "ignore",
        )

    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "ignore"
    ):
        if expression.keywords or len(expression.args) != 1:
            raise _InvalidIgnoreSyntax
        argument = expression.args[0]
        if isinstance(argument, ast.Name) and argument.id in _BUILTIN_PATTERNS:
            return _ParsedVariableHead(name="ignore", is_ignore=True)
        if isinstance(argument, ast.Constant) and type(argument.value) is str:
            try:
                _validate_regex(argument.value, limits)
            except ValueError as exc:
                raise _InvalidIgnoreSyntax from exc
            return _ParsedVariableHead(name="ignore", is_ignore=True)
        raise _InvalidIgnoreSyntax

    if mentions_ignore or any(
        isinstance(node, ast.Name) and node.id == "ignore"
        for node in ast.walk(expression)
    ):
        raise _InvalidIgnoreSyntax
    raise ValueError("variable must be a simple name or supported ignore call")


def _invalid_ignore_issue() -> ValidationIssue:
    return _issue(
        "ttp.invalid_ignore_syntax",
        "TTP ignore must be bare or called once with a built-in pattern or regex",
        details={"required_action": "replace_with_ignore_call"},
    )


def _validate_attribute(attribute: _ParsedAttribute, limits: _TtpLimits) -> None:
    name = attribute.name
    args = attribute.args
    kwargs = attribute.kwargs
    if name not in _ALLOWED_ATTRIBUTES:
        raise ValueError("variable attribute is not permitted")
    if kwargs:
        raise ValueError("keyword arguments are not permitted")
    if name in _NO_ARGUMENT_ATTRIBUTES and args:
        raise ValueError(f"attribute {name!r} does not accept arguments")
    if name in _STRING_CONDITIONS and not args:
        raise ValueError(f"attribute {name!r} requires a string argument")
    if name in _STRING_CONDITIONS and any(not isinstance(arg, str) for arg in args):
        raise ValueError(f"attribute {name!r} only accepts string arguments")
    if name in _REGEX_CONDITIONS:
        if len(args) != 1:
            raise ValueError(f"attribute {name!r} requires one regex argument")
        _validate_regex(args[0], limits)
    if name == "re":
        if len(args) != 1:
            raise ValueError("re requires one regular expression argument")
        _validate_regex(args[0], limits)
    if name == "joinmatches" and (
        len(args) > 1 or (args and not isinstance(args[0], str))
    ):
        raise ValueError("joinmatches accepts at most one string separator")
    if name == "item":
        if len(args) != 1 or isinstance(args[0], bool) or not isinstance(args[0], int):
            raise ValueError("item requires one integer index")
        if not -10_000 <= args[0] <= 10_000:
            raise ValueError("item index is outside the supported range")
    if name in {"to_ip", "to_net", "is_ip"} and (
        len(args) > 1 or any(arg not in {"ipv4", "ipv6"} for arg in args)
    ):
        raise ValueError(f"attribute {name!r} only accepts ipv4 or ipv6")


def _validate_group_name(value: str) -> bool:
    candidate = value[1:] if value.startswith("/") else value
    return bool(candidate) and all(
        _GROUP_SEGMENT_RE.fullmatch(segment) for segment in candidate.split(".")
    )


def _split_variable_pipeline(expression: str) -> list[str]:
    """Split TTP pipes without treating pipes inside string literals as syntax."""

    try:
        tokens = tokenize.generate_tokens(StringIO(expression).readline)
        parts: list[str] = []
        current: list[tuple[int, str]] = []
        depth = 0
        for token in tokens:
            if token.type == tokenize.ENDMARKER:
                break
            if token.type == tokenize.OP:
                if token.string in "([{":
                    depth += 1
                elif token.string in ")]}":
                    depth -= 1
                    if depth < 0:
                        raise ValueError("unbalanced pipeline expression")
                elif token.string == "|" and depth == 0:
                    parts.append(tokenize.untokenize(current).strip())
                    current = []
                    continue
            current.append((token.type, token.string))
    except (IndentationError, tokenize.TokenError) as exc:
        raise ValueError("invalid pipeline expression") from exc

    if depth != 0:
        raise ValueError("unbalanced pipeline expression")
    parts.append(tokenize.untokenize(current).strip())
    return parts


def _walk_groups(
    element: ET.Element,
    *,
    depth: int,
    result_depth: int,
    path: tuple[int, ...],
    issues: list[ValidationIssue],
    limits: _TtpLimits,
) -> None:
    if len(issues) >= MAX_TTP_VALIDATION_ISSUES:
        return
    if depth > limits.group_depth:
        issues.append(
            _issue(
                "ttp.group_depth_exceeded",
                f"TTP group nesting exceeds {limits.group_depth}",
                path="/template" + "".join(f"/group[{index}]" for index in path),
            ),
        )
        return

    if element.tag != "group":
        details = {}
        if isinstance(element.tag, str) and _XML_TAG_RE.fullmatch(element.tag):
            details["tag"] = element.tag[:128]
        issues.append(
            _issue(
                "ttp.forbidden_tag",
                "only group elements are permitted inside a TTP template",
                path="/template",
                details=details,
            ),
        )
        return

    allowed_attributes = {"name", "method"} | _GROUP_FILTERS
    element_path = "/template" + "".join(f"/group[{index}]" for index in path)
    child_result_depth = result_depth
    for name, value in element.attrib.items():
        attribute_path = f"{element_path}/@{name}"
        if name not in allowed_attributes:
            issues.append(
                _issue(
                    "ttp.forbidden_group_attribute",
                    "group contains a forbidden attribute",
                    path=attribute_path,
                ),
            )
            continue
        if len(value) > limits.attribute_chars:
            issues.append(
                _issue(
                    "ttp.group_attribute_too_long",
                    "group attribute value is too long",
                    path=attribute_path,
                ),
            )
            continue
        if name == "name":
            if not _validate_group_name(value):
                issues.append(
                    _issue(
                        "ttp.invalid_group_name",
                        "group names must use snake_case path segments "
                        "and optional '*' suffixes",
                        path=attribute_path,
                    ),
                )
            else:
                segment_count = len(value.lstrip("/").split("."))
                child_result_depth = (
                    segment_count
                    if value.startswith("/")
                    else result_depth + segment_count
                )
                if child_result_depth > limits.group_depth:
                    issues.append(
                        _issue(
                            "ttp.group_depth_exceeded",
                            f"TTP result nesting exceeds {limits.group_depth}",
                            path=attribute_path,
                        ),
                    )
        elif name == "method" and value not in {"group", "table"}:
            issues.append(
                _issue(
                    "ttp.invalid_group_method",
                    "group method must be group or table",
                    path=attribute_path,
                ),
            )
        elif name in _GROUP_FILTERS:
            try:
                parsed = _parse_attribute(f"{name}({value})", limits)
                if parsed.kwargs or not parsed.args:
                    raise ValueError("group filter requires literal arguments")
                if any(not isinstance(argument, str) for argument in parsed.args):
                    raise ValueError("group filter arguments must be strings")
                if name == "equal" and len(parsed.args) != 2:
                    raise ValueError("equal requires a field and value")
            except ValueError:
                issues.append(
                    _issue(
                        "ttp.unsafe_group_attribute",
                        "group filter contains unsupported or unsafe arguments",
                        path=attribute_path,
                    ),
                )

    for index, child in enumerate(element):
        _walk_groups(
            child,
            depth=depth + 1,
            result_depth=child_result_depth,
            path=(*path, index),
            issues=issues,
            limits=limits,
        )


def _iter_template_text(element: ET.Element) -> Sequence[str]:
    """Iterate XML text without recursion on attacker-controlled nesting."""

    chunks: list[str] = []
    stack: list[tuple[ET.Element, bool]] = [(element, False)]
    while stack:
        current, is_tail = stack.pop()
        if is_tail:
            if current.tail:
                chunks.append(current.tail)
            continue
        if current.text:
            chunks.append(current.text)
        for child in reversed(list(current)):
            stack.append((child, True))
            stack.append((child, False))
    return chunks


def inspect_ttp_template(
    template: str,
    *,
    max_ttp_template_bytes: int = MAX_TTP_TEMPLATE_BYTES,
    max_ttp_group_depth: int = MAX_TTP_GROUP_DEPTH,
    max_ttp_regex_chars: int = MAX_TTP_REGEX_CHARS,
    max_ttp_argument_chars: int = MAX_TTP_ARGUMENT_CHARS,
) -> list[ValidationIssue]:
    """Reject unsafe TTP syntax before the TTP package sees the template."""

    limits = _TtpLimits(
        template_bytes=_effective_limit(
            max_ttp_template_bytes,
            MAX_TTP_TEMPLATE_BYTES,
            "max_ttp_template_bytes",
        ),
        group_depth=_effective_limit(
            max_ttp_group_depth,
            MAX_TTP_GROUP_DEPTH,
            "max_ttp_group_depth",
        ),
        regex_chars=_effective_limit(
            max_ttp_regex_chars,
            MAX_TTP_REGEX_CHARS,
            "max_ttp_regex_chars",
        ),
        argument_chars=_effective_limit(
            max_ttp_argument_chars,
            MAX_TTP_ARGUMENT_CHARS,
            "max_ttp_argument_chars",
        ),
    )
    if not isinstance(template, str) or not template:
        return [_issue("ttp.empty_template", "TTP template must not be empty")]
    try:
        size = len(template.encode("utf-8"))
    except UnicodeEncodeError:
        return [_issue("ttp.invalid_utf8", "TTP template must be valid UTF-8 text")]
    if size > limits.template_bytes:
        return [
            _issue(
                "ttp.template_too_large",
                f"TTP template exceeds {limits.template_bytes} UTF-8 bytes",
            ),
        ]
    if _DANGEROUS_XML_RE.search(template):
        return [
            _issue(
                "ttp.forbidden_xml_declaration",
                "XML declarations, processing instructions, DTDs, "
                "and entities are forbidden",
            ),
        ]

    source = template
    try:
        root = ET.fromstring(source)
        if root.tag != "template":
            source = f"<template>\n{template}\n</template>"
            root = ET.fromstring(source)
    except ET.ParseError:
        try:
            source = f"<template>\n{template}\n</template>"
            root = ET.fromstring(source)
        except ET.ParseError as error:
            line, column = getattr(error, "position", (0, 0))
            return [
                _issue(
                    "ttp.invalid_xml",
                    "TTP template is not well-formed XML; escape literal XML "
                    "metacharacters in match text",
                    details={
                        "column": max(0, int(column)),
                        "line": max(0, int(line)),
                        "required_action": "escape_xml_metacharacters",
                    },
                ),
            ]

    issues: list[ValidationIssue] = []
    if root.tag != "template":
        issues.append(
            _issue(
                "ttp.invalid_root_tag",
                "explicit TTP XML must use a template root",
                path="/",
            ),
        )
        return issues
    if root.attrib:
        issues.append(
            _issue(
                "ttp.forbidden_template_attribute",
                "template root attributes are not permitted",
                path="/template",
            ),
        )
    for index, child in enumerate(root):
        if len(issues) >= MAX_TTP_VALIDATION_ISSUES:
            break
        _walk_groups(
            child,
            depth=1,
            result_depth=0,
            path=(index,),
            issues=issues,
            limits=limits,
        )

    variable_count = 0
    for chunk in _iter_template_text(root):
        if len(issues) >= MAX_TTP_VALIDATION_ISSUES:
            break
        for line in chunk.splitlines():
            line_variables: set[str] = set()
            for line_match in _VARIABLE_RE.finditer(line):
                try:
                    line_parts = _split_variable_pipeline(
                        line_match.group(1).strip(),
                    )
                    if not line_parts or not line_parts[0]:
                        continue
                    line_head = _parse_variable_head(line_parts[0], limits)
                except ValueError:
                    continue
                line_variable = line_head.name
                if not line_head.is_ignore and line_variable in line_variables:
                    details = {}
                    if len(line_variable) <= 128:
                        details["variable"] = line_variable
                    issues.append(
                        _issue(
                            "ttp.duplicate_line_variable",
                            "a TTP variable name can appear only once on a "
                            "physical template line",
                            details=details,
                        ),
                    )
                line_variables.add(line_variable)
        matches = list(_VARIABLE_RE.finditer(chunk))
        without_variables = _VARIABLE_RE.sub("", chunk)
        if "{{" in without_variables or "}}" in without_variables:
            issues.append(
                _issue(
                    "ttp.invalid_variable_syntax",
                    "TTP variable delimiters are unbalanced or nested",
                ),
            )
            continue
        variable_count += len(matches)
        for match in matches:
            expression = match.group(1).strip()
            try:
                parts = _split_variable_pipeline(expression)
            except ValueError:
                parts = []
            if not parts or any(not part for part in parts):
                issues.append(
                    _issue(
                        "ttp.invalid_variable_syntax",
                        "TTP variable pipeline contains an empty component",
                    ),
                )
                continue
            try:
                variable_head = _parse_variable_head(parts[0], limits)
            except _InvalidIgnoreSyntax:
                issues.append(_invalid_ignore_issue())
                continue
            except ValueError:
                issues.append(
                    _issue(
                        "ttp.invalid_field_name",
                        "TTP result field names must be ASCII snake_case",
                    ),
                )
                continue
            variable_name = variable_head.name
            if variable_head.is_ignore and len(parts) != 1:
                issues.append(_invalid_ignore_issue())
                continue
            special_names = {
                "_end_",
                "_headers_",
                "_line_",
                "_start_",
                "ignore",
            }
            if variable_name in {"_exact_", "_exact_space_"}:
                issues.append(
                    _issue(
                        "ttp.invalid_line_control",
                        "exact line controls must modify a real result field",
                        details={
                            "control": variable_name,
                            "required_action": "attach_to_schema_field",
                        },
                    ),
                )
                continue
            if variable_name not in special_names and not _FIELD_NAME_RE.fullmatch(
                variable_name
            ):
                issues.append(
                    _issue(
                        "ttp.invalid_field_name",
                        "TTP result field names must be ASCII snake_case",
                    ),
                )
                continue
            for raw_attribute in parts[1:]:
                attribute: _ParsedAttribute | None = None
                try:
                    attribute = _parse_attribute(raw_attribute, limits)
                    if attribute.name == "ignore":
                        issues.append(_invalid_ignore_issue())
                        break
                    _validate_attribute(attribute, limits)
                except ValueError:
                    details = {}
                    if attribute is not None:
                        details["attribute"] = attribute.name[:128]
                    issues.append(
                        _issue(
                            "ttp.unsafe_variable_attribute",
                            "TTP variable uses an unsupported or unsafe "
                            "attribute expression",
                            details=details,
                        ),
                    )

    if variable_count == 0:
        issues.append(
            _issue(
                "ttp.no_variables",
                "TTP template must contain at least one match variable",
            ),
        )
    return issues[:MAX_TTP_VALIDATION_ISSUES]


def _normalise_json_value(value: Any) -> Any:
    if isinstance(
        value,
        (
            ipaddress.IPv4Address,
            ipaddress.IPv4Interface,
            ipaddress.IPv4Network,
            ipaddress.IPv6Address,
            ipaddress.IPv6Interface,
            ipaddress.IPv6Network,
        ),
    ):
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not float("-inf") < value < float("inf"):
            raise ValueError("non-finite result")
        return value
    if isinstance(value, list):
        return [_normalise_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_normalise_json_value(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("non-string result key")
        return {key: _normalise_json_value(item) for key, item in value.items()}
    raise ValueError("non-JSON result value")


def _isolated_ttp_worker(
    connection: Any,
    template: str,
    command_outputs: tuple[str, ...],
    max_result_bytes: int,
) -> None:
    try:
        logging.disable(logging.CRITICAL)
        os.environ.pop("TTP_TEMPLATES_DIR", None)
        with (
            tempfile.TemporaryDirectory(
                prefix="cli-parser-ttp-",
            ) as cache_dir,
            open(os.devnull, "w", encoding="utf-8") as devnull,
        ):
            os.environ["TTPCACHEFOLDER"] = cache_dir
            with (
                contextlib.redirect_stdout(devnull),
                contextlib.redirect_stderr(
                    devnull,
                ),
            ):
                from ttp import ttp

                parser = ttp(template=f"\n{template}")
                for index, output in enumerate(command_outputs):
                    parser.add_input(
                        f"\n{output}",
                        input_name=f"sample_{index}",
                        template_name="_all_",
                    )
                parser.parse(one=True)
                result = parser.result(structure="list")

        if not isinstance(result, list) or len(result) != 1:
            connection.send(("invalid_shape", None))
            return
        records = _normalise_json_value(result[0])
        if not isinstance(records, list) or len(records) != len(command_outputs):
            connection.send(("invalid_mapping", None))
            return
        if any(not isinstance(record, dict) for record in records):
            connection.send(("invalid_record", None))
            return
        payload_size = len(
            json.dumps(
                records,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )
        if payload_size > max_result_bytes:
            connection.send(("result_too_large", None))
            return
        connection.send(("ok", records))
    except BaseException as exc:  # child must convert SystemExit and parser failures
        with contextlib.suppress(BaseException):
            connection.send(("worker_error", type(exc).__name__))
    finally:
        connection.close()


def _stop_process(process: multiprocessing.Process) -> None:
    try:
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.1)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=0.1)
    except (AssertionError, OSError, ValueError):
        return


def _spawn_host_is_importable() -> bool:
    """Return whether multiprocessing spawn can re-import the host module."""

    if getattr(sys, "frozen", False):
        return True
    main_module = sys.modules.get("__main__")
    main_file = getattr(main_module, "__file__", None)
    if not isinstance(main_file, str) or not main_file:
        return False
    return not main_file.startswith("<") and main_file not in {"-c", "-"}


def _run_ttp_isolated(
    template: str,
    command_outputs: Sequence[str],
    *,
    timeout_seconds: float,
    max_result_bytes: int,
) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    if timeout_seconds <= 0:
        return [], [
            _issue(
                "ttp.timeout",
                "isolated TTP parsing exceeded its time limit",
            ),
        ]
    if not _spawn_host_is_importable():
        return [], [
            _issue(
                "ttp.worker_host_unsupported",
                "isolated TTP parsing requires an importable Python host module",
            ),
        ]

    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_isolated_ttp_worker,
        args=(send, template, tuple(command_outputs), max_result_bytes),
        daemon=True,
    )
    started = time.monotonic()
    try:
        process.start()
    except (OSError, RuntimeError, AssertionError, ValueError):
        receive.close()
        send.close()
        return [], [
            _issue(
                "ttp.worker_start_failed",
                "isolated TTP parser process could not be started",
            ),
        ]
    send.close()

    try:
        deadline = started + timeout_seconds
        status, payload = "worker_error", None
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                status, payload = "timeout", None
                break
            if receive.poll(min(0.05, remaining)):
                try:
                    status, payload = receive.recv()
                except EOFError:
                    status, payload = "worker_bootstrap_failed", None
                if time.monotonic() > deadline:
                    status, payload = "timeout", None
                break
            if not process.is_alive():
                status, payload = "worker_bootstrap_failed", None
                break
    finally:
        receive.close()
        if status in {"timeout", "worker_bootstrap_failed"}:
            _stop_process(process)
        else:
            process.join(timeout=0.1)
            _stop_process(process)
        with contextlib.suppress(OSError, ValueError):
            process.close()

    messages = {
        "invalid_shape": "TTP template produced an unsupported template result shape",
        "invalid_mapping": "TTP results do not map one-to-one to command outputs",
        "invalid_record": "each command output must produce exactly one root object",
        "result_too_large": "TTP parsing result exceeds the configured size limit",
        "worker_error": "isolated TTP parsing failed",
        "worker_bootstrap_failed": (
            "isolated TTP parser process exited before reporting a result"
        ),
        "worker_host_unsupported": (
            "isolated TTP parsing requires an importable Python host module"
        ),
        "timeout": "isolated TTP parsing exceeded its time limit",
    }
    if status != "ok":
        details = {}
        if (
            status == "worker_error"
            and isinstance(payload, str)
            and len(payload) <= 128
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", payload)
        ):
            details["exception_type"] = payload
        return [], [
            _issue(
                f"ttp.{status}" if status in messages else "ttp.worker_error",
                messages.get(status, messages["worker_error"]),
                details=details,
            ),
        ]
    return payload, []


def _string_has_source(value: str, source: str) -> bool:
    if not value:
        return False
    if value in source:
        return True
    # joinmatches may combine captures while preserving each non-trivial fragment.
    fragments = [fragment for fragment in re.split(r"[\s,;|]+", value) if fragment]
    return bool(fragments) and all(fragment in source for fragment in fragments)


def _number_has_source(value: int | float, source: str) -> bool:
    for match in _NUMBER_TOKEN_RE.finditer(source):
        token = match.group(0)
        try:
            matches_integer = (
                isinstance(value, int)
                and not isinstance(value, bool)
                and not any(marker in token.lower() for marker in (".", "e"))
                and int(token) == value
            )
            matches_number = isinstance(value, float) and float(token) == value
            if matches_integer or matches_number:
                return True
        except (OverflowError, ValueError):
            continue
    return False


def _scalar_has_source(value: Any, source: str) -> bool:
    if isinstance(value, str):
        return _string_has_source(value, source)
    if isinstance(value, bool):
        expected = {"true", "yes", "on", "1"} if value else {"false", "no", "off", "0"}
        tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9]+", source)}
        return bool(expected & tokens)
    if isinstance(value, (int, float)):
        return _number_has_source(value, source)
    return False


def _validate_scalar_sources(
    value: Any,
    source: str,
    *,
    output_index: int,
    path: tuple[str, ...] = (),
    max_issues: int = MAX_TTP_VALIDATION_ISSUES,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    stack: list[tuple[Any, tuple[str, ...]]] = [(value, path)]
    while stack and len(issues) < max_issues:
        current, current_path = stack.pop()
        if isinstance(current, dict):
            for key, child in reversed(list(current.items())):
                stack.append((child, (*current_path, str(key))))
        elif isinstance(current, list):
            for child in reversed(current):
                stack.append((child, (*current_path, "*")))
        elif not _scalar_has_source(current, source):
            pointer = "/" + "/".join(current_path) if current_path else "/"
            issues.append(
                _issue(
                    "ttp.scalar_without_source",
                    "parsed scalar cannot be conservatively traced to the "
                    "command output",
                    path=pointer,
                    output_index=output_index,
                    stage="acceptance",
                ),
            )
    return issues


def validate_ttp_template(
    template: str,
    command_outputs: Sequence[str],
    result_schema: Mapping[str, Any],
    *,
    timeout_seconds: float = DEFAULT_TTP_TIMEOUT_SECONDS,
    max_result_bytes: int = MAX_TTP_RESULT_BYTES,
    max_ttp_template_bytes: int = MAX_TTP_TEMPLATE_BYTES,
    max_ttp_group_depth: int = MAX_TTP_GROUP_DEPTH,
    max_ttp_regex_chars: int = MAX_TTP_REGEX_CHARS,
    max_ttp_argument_chars: int = MAX_TTP_ARGUMENT_CHARS,
    max_schema_bytes: int = MAX_SCHEMA_BYTES,
    max_schema_depth: int = MAX_SCHEMA_DEPTH,
    max_schema_properties: int = MAX_SCHEMA_PROPERTIES,
) -> TtpValidationResult:
    """Inspect, parse, and fully validate one TTP candidate against all inputs."""

    max_result_bytes = _effective_limit(
        max_result_bytes,
        MAX_TTP_RESULT_BYTES,
        "max_result_bytes",
    )
    schema_limits = {
        "max_schema_bytes": max_schema_bytes,
        "max_schema_depth": max_schema_depth,
        "max_schema_properties": max_schema_properties,
    }
    schema_issues = validate_records_against_schema(
        [],
        result_schema,
        **schema_limits,
    )
    if schema_issues:
        return TtpValidationResult(records=[], issues=schema_issues)
    static_issues = inspect_ttp_template(
        template,
        max_ttp_template_bytes=max_ttp_template_bytes,
        max_ttp_group_depth=max_ttp_group_depth,
        max_ttp_regex_chars=max_ttp_regex_chars,
        max_ttp_argument_chars=max_ttp_argument_chars,
    )
    if static_issues:
        return TtpValidationResult(records=[], issues=static_issues)
    if not command_outputs:
        return TtpValidationResult(
            records=[],
            issues=[_issue("ttp.no_inputs", "at least one command output is required")],
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds < 0
    ):
        return TtpValidationResult(
            records=[],
            issues=[_issue("ttp.invalid_timeout", "TTP timeout cannot be negative")],
        )

    records, issues = _run_ttp_isolated(
        template,
        command_outputs,
        timeout_seconds=timeout_seconds,
        max_result_bytes=max_result_bytes,
    )
    if issues:
        return TtpValidationResult(records=[], issues=issues)

    no_match_indexes = {
        output_index for output_index, record in enumerate(records) if not record
    }
    issues = [
        _issue(
            "ttp.no_match",
            "TTP template produced an empty root object; simplify the first "
            "group match line so it matches this command output",
            output_index=output_index,
        )
        for output_index in sorted(no_match_indexes)
    ]
    issues.extend(
        issue
        for issue in validate_records_against_schema(
            records,
            result_schema,
            **schema_limits,
        )
        if issue.output_index not in no_match_indexes
    )
    pairs = zip(records, command_outputs, strict=True)
    for output_index, (record, source) in enumerate(pairs):
        remaining_issues = MAX_TTP_VALIDATION_ISSUES - len(issues)
        if remaining_issues <= 0:
            break
        issues.extend(
            _validate_scalar_sources(
                record,
                source,
                output_index=output_index,
                max_issues=remaining_issues,
            ),
        )
    return TtpValidationResult(
        records=records,
        issues=issues[:MAX_TTP_VALIDATION_ISSUES],
    )


__all__ = [
    "DEFAULT_TTP_TIMEOUT_SECONDS",
    "MAX_TTP_GROUP_DEPTH",
    "MAX_TTP_ARGUMENT_CHARS",
    "MAX_TTP_REGEX_CHARS",
    "MAX_TTP_RESULT_BYTES",
    "MAX_TTP_TEMPLATE_BYTES",
    "TtpValidationResult",
    "inspect_ttp_template",
    "validate_ttp_template",
]
