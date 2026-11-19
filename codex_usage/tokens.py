"""Raw token counters and their mutually exclusive composition."""

from __future__ import annotations

from typing import Any


RAW_TOKEN_COLUMNS = [
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
]

COMPOSITION_COLUMNS = [
    "uncached_input_tokens",
    "cached_input_component",
    "cache_write_input_component",
    "visible_output_tokens",
    "reasoning_output_component",
    "unclassified_tokens",
]

def safe_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def token_values(payload: dict[str, Any]) -> dict[str, int]:
    values = {column: safe_int(payload.get(column)) for column in RAW_TOKEN_COLUMNS}
    if values["total_tokens"] == 0:
        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    return values


def subtract_usage(
    current: dict[str, int], previous: dict[str, int] | None
) -> dict[str, int]:
    if previous is None:
        return current.copy()
    delta: dict[str, int] = {}
    for column in RAW_TOKEN_COLUMNS:
        difference = current[column] - previous.get(column, 0)
        # A negative difference means the cumulative counter reset.  In that
        # case the current value is the safest local estimate for this event.
        delta[column] = difference if difference >= 0 else current[column]
    if delta["total_tokens"] == 0:
        delta["total_tokens"] = delta["input_tokens"] + delta["output_tokens"]
    return delta


def token_composition(values: dict[str, int]) -> dict[str, int]:
    """Convert overlapping counters into mutually exclusive chart segments."""
    total = values["total_tokens"]
    input_component = min(values["input_tokens"], total)
    remaining = max(total - input_component, 0)
    output_component = min(values["output_tokens"], remaining)

    cached = min(values["cached_input_tokens"], input_component)
    cache_write = min(
        values["cache_write_input_tokens"], max(input_component - cached, 0)
    )
    uncached = max(input_component - cached - cache_write, 0)

    reasoning = min(values["reasoning_output_tokens"], output_component)
    visible_output = max(output_component - reasoning, 0)
    unclassified = max(total - input_component - output_component, 0)

    return {
        "uncached_input_tokens": uncached,
        "cached_input_component": cached,
        "cache_write_input_component": cache_write,
        "visible_output_tokens": visible_output,
        "reasoning_output_component": reasoning,
        "unclassified_tokens": unclassified,
    }
