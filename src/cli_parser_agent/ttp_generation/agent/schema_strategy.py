"""Private request-local experiment factory; no environment/product switch."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

SchemaStrategy = Literal["direct", "plan", "plan_confirm"]
_strategy: ContextVar[SchemaStrategy] = ContextVar("schema_strategy", default="direct")
_VERSIONS = {
    "direct": "ttp-generator-v44-schema-runtime-contract-zh-cn",
    "plan": "ttp-generator-v45-schema-plan-compiler-zh-cn",
    "plan_confirm": "ttp-generator-v46-schema-plan-confirmation-zh-cn",
}


def current_schema_strategy() -> SchemaStrategy:
    return _strategy.get()


def current_prompt_version() -> str:
    return _VERSIONS[current_schema_strategy()]


@contextmanager
def schema_strategy_for_testing(strategy: SchemaStrategy) -> Iterator[None]:
    if strategy not in _VERSIONS:
        raise ValueError("unsupported schema strategy")
    token = _strategy.set(strategy)
    try:
        yield
    finally:
        _strategy.reset(token)
