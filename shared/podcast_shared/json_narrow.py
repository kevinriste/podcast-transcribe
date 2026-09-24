"""Type guards for narrowing decoded JSON (``json.loads`` returns ``Any``)."""

from __future__ import annotations

from typing import TypeGuard


def is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """Narrow a decoded JSON value to an object (JSON keys are always strings).

    Returns:
        True when ``value`` is a dict.

    """
    return isinstance(value, dict)


def is_json_array(value: object) -> TypeGuard[list[object]]:
    """Narrow a decoded JSON value to an array.

    Returns:
        True when ``value`` is a list.

    """
    return isinstance(value, list)
