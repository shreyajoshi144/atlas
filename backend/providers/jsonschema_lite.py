"""Tiny JSON-Schema subset validator (type, required, properties, items, enum,
additionalProperties, min/maxLength, min/maxItems). Enough for Atlas's own
structured-output contracts without adding a dependency."""
from __future__ import annotations

from typing import Any

_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    if t == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif t in _TYPES:
        ok = isinstance(value, _TYPES[t])
    else:
        ok = True
    if not ok:
        return [f"{path}: expected {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: not one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errs.append(f"{path}: too short")
        if len(value) > schema.get("maxLength", 10**9):
            errs.append(f"{path}: too long")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errs.append(f"{path}: too few items")
        if len(value) > schema.get("maxItems", 10**9):
            errs.append(f"{path}: too many items")
        if "items" in schema:
            for i, v in enumerate(value):
                errs += validate(v, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in value:
                errs.append(f"{path}.{r}: missing")
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}.{k}: unexpected property")
    return errs
