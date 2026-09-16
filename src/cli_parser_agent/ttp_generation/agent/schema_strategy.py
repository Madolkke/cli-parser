"""Request-local experimental Schema selection; public requests stay unchanged."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from .prompt import PROMPT_VERSION

SchemaStrategy = Literal["direct", "draft"]
_strategy: ContextVar[SchemaStrategy] = ContextVar("schema_strategy", default="direct")
DRAFT_PROMPT_VERSION = "ttp-generator-v47-lightweight-label-and-root-structure-zh-cn"


def current_schema_strategy() -> SchemaStrategy:
    return _strategy.get()


def current_prompt_version() -> str:
    return (
        DRAFT_PROMPT_VERSION if current_schema_strategy() == "draft" else PROMPT_VERSION
    )


@contextmanager
def schema_strategy_for_testing(strategy: SchemaStrategy) -> Iterator[None]:
    if strategy not in {"direct", "draft"}:
        raise ValueError("Unknown internal Schema strategy")
    token = _strategy.set(strategy)
    try:
        yield
    finally:
        _strategy.reset(token)
