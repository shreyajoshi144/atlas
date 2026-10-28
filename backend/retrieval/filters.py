"""Metadata filtering for retrieval candidates.

A filter is a plain mapping, MongoDB/Chroma-flavoured::

    {"topic": "batteries"}                       # equality
    {"domain": {"$in": ["nature.com", "arxiv.org"]}}
    {"published": {"$gte": "2025-01-01"}}        # ISO dates compare as dates
    {"credibility": {"$gte": 60}, "$or": [{"kind": "a"}, {"kind": "b"}]}

Top-level keys are AND-ed. Unknown operators raise :class:`InvalidFilterError`
instead of being ignored: silently dropping a filter would widen a query's
results, which is unacceptable when the filter expresses scoping.

Semantics for a *missing* field follow MongoDB: equality, ``$in``, ``$exists:
true`` and the range operators do not match; ``$ne``, ``$nin`` and ``$exists:
false`` do.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

_MISSING = object()
_OPS = {"$eq", "$ne", "$in", "$nin", "$gt", "$gte", "$lt", "$lte", "$exists"}


class InvalidFilterError(ValueError):
    """The supplied metadata filter is malformed."""


def _as_comparable(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
            except ValueError:
                try:
                    return date.fromisoformat(text[:10])
                except ValueError:
                    return text
        return text
    return value


def _compare(actual: Any, expected: Any, op: str) -> bool:
    a, e = _as_comparable(actual), _as_comparable(expected)
    try:
        if op == "$gt":
            return a > e
        if op == "$gte":
            return a >= e
        if op == "$lt":
            return a < e
        return a <= e  # $lte
    except TypeError:
        return False


def _eq(actual: Any, expected: Any) -> bool:
    return _as_comparable(actual) == _as_comparable(expected)


def _match_field(actual: Any, condition: Any) -> bool:
    if not (isinstance(condition, Mapping) and any(str(k).startswith("$") for k in condition)):
        return actual is not _MISSING and _eq(actual, condition)
    for op, expected in condition.items():
        if op not in _OPS:
            raise InvalidFilterError(f"Unsupported filter operator: {op!r}")
        missing = actual is _MISSING
        if op == "$exists":
            if bool(expected) == missing:
                return False
        elif op == "$eq":
            if missing or not _eq(actual, expected):
                return False
        elif op == "$ne":
            if not missing and _eq(actual, expected):
                return False
        elif op in {"$in", "$nin"}:
            if not isinstance(expected, (list, tuple, set, frozenset)):
                raise InvalidFilterError(f"{op} requires a list of values")
            present = (not missing) and any(_eq(actual, item) for item in expected)
            if op == "$in" and not present:
                return False
            if op == "$nin" and present:
                return False
        else:  # range operators
            if missing or not _compare(actual, expected, op):
                return False
    return True


def validate_filter(spec: Mapping[str, Any] | None) -> None:
    """Raise :class:`InvalidFilterError` if ``spec`` is malformed (cheap, eager)."""
    matches_filter({}, spec)


def matches_filter(metadata: Mapping[str, Any], spec: Mapping[str, Any] | None) -> bool:
    if not spec:
        return True
    if not isinstance(spec, Mapping):
        raise InvalidFilterError("A metadata filter must be a mapping")
    for key, condition in spec.items():
        if key == "$and":
            if not isinstance(condition, (list, tuple)):
                raise InvalidFilterError("$and requires a list of filters")
            if not all(matches_filter(metadata, sub) for sub in condition):
                return False
        elif key == "$or":
            if not isinstance(condition, (list, tuple)) or not condition:
                raise InvalidFilterError("$or requires a non-empty list of filters")
            if not any(matches_filter(metadata, sub) for sub in condition):
                return False
        elif str(key).startswith("$"):
            raise InvalidFilterError(f"Unsupported top-level operator: {key!r}")
        else:
            if not _match_field(metadata.get(key, _MISSING), condition):
                return False
    return True
