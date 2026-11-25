"""Validation helpers for the open RunLedger receipt format.

The base install stays dependency-free, so this module implements the small JSON Schema
subset used by receipt.schema.json. It is a conformance check for producers and consumers,
not a general-purpose JSON Schema validator.
"""
from __future__ import annotations

import json
import math
from importlib import resources
from typing import Any, Dict, List

SCHEMA: Dict[str, Any] = json.loads(
    resources.files("runledger").joinpath("receipt.schema.json").read_text(encoding="utf-8")
)

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: (isinstance(v, int) or isinstance(v, float) and math.isfinite(v) and v.is_integer())
                         and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: Dict[str, Any], path: str = "$", root: Dict[str, Any] = SCHEMA) -> List[str]:
    """Validate `value` against the receipt schema subset and return human-readable errors.

    Supported keywords: local `$ref`, `type`, `enum`, `required`, `properties`,
    schema-valued `additionalProperties`, `items`, `minimum` and `maximum`.
    """
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return validate(value, node, path, root)
    errors: List[str] = []
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        if not any(_TYPES[t](value) for t in types):
            return [f"{path}: value is not {'/'.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value not in {schema['enum']}")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > {schema['maximum']}")
    if isinstance(value, dict):
        errors.extend(f"{path}: missing {key}" for key in schema.get("required", []) if key not in value)
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                errors.extend(validate(item, props[key], f"{path}.{key}", root))
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(validate(item, schema["additionalProperties"], f"{path}.{key}", root))
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]", root))
    return errors


def validate_receipt(value: Any) -> List[str]:
    """Conformance errors for one decoded JSON receipt; empty means valid."""
    return validate(value, SCHEMA)
