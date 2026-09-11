"""Small JSON-schema-subset validator for agent output contracts.

The subset is the one CALL-E accepts for ``result_schema`` (see
``04_CALL_E_INTEGRATION.md``): ``type``, ``properties``, ``required``,
``enum``, nested ``object``, simple ``array.items``, ``description`` and
``additionalProperties: false``. Anything else (``$ref``, ``oneOf``,
``anyOf``, ``allOf``, ``additionalProperties: true``, ``format``, ...) is
rejected up front, so a schema that passes here is also safe to hand to the
call layer later. No external dependency.
"""

from __future__ import annotations

from typing import Any

ALLOWED_TYPES: frozenset[str] = frozenset(
    {"object", "string", "number", "integer", "boolean", "array"}
)
ALLOWED_KEYS: frozenset[str] = frozenset(
    {"type", "properties", "required", "enum", "description", "additionalProperties", "items"}
)
MAX_DEPTH = 6


class SchemaError(ValueError):
    """The schema itself is outside the supported subset."""


def _check_node(node: Any, path: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise SchemaError(f"{path}: nesting deeper than {MAX_DEPTH} is not supported")
    if not isinstance(node, dict):
        raise SchemaError(f"{path}: schema node must be an object")
    unknown = sorted(set(node) - ALLOWED_KEYS)
    if unknown:
        raise SchemaError(f"{path}: unsupported schema keys {unknown}")
    node_type = node.get("type")
    if not isinstance(node_type, str):
        raise SchemaError(f"{path}: 'type' must be a single string")
    if node_type not in ALLOWED_TYPES:
        raise SchemaError(f"{path}: 'type' must be one of {sorted(ALLOWED_TYPES)}")
    if "description" in node and not isinstance(node["description"], str):
        raise SchemaError(f"{path}: 'description' must be a string")
    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not enum:
            raise SchemaError(f"{path}: 'enum' must be a non-empty list")
        if node_type != "string" or not all(isinstance(v, str) for v in enum):
            raise SchemaError(f"{path}: 'enum' is supported for string types only")
    if node_type == "object":
        properties = node.get("properties")
        if not isinstance(properties, dict):
            raise SchemaError(f"{path}: object requires a 'properties' mapping")
        if node.get("additionalProperties", None) is not False:
            raise SchemaError(f"{path}: object must declare 'additionalProperties': false")
        required = node.get("required", [])
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            raise SchemaError(f"{path}: 'required' must be a list of property names")
        missing = [r for r in required if r not in properties]
        if missing:
            raise SchemaError(f"{path}: required properties {missing} are not defined")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise SchemaError(f"{path}: property names must be non-empty strings")
            _check_node(child, f"{path}.{name}", depth + 1)
    else:
        for key in ("properties", "required", "additionalProperties"):
            if key in node:
                raise SchemaError(f"{path}: '{key}' is only valid on object types")
    if node_type == "array":
        if "items" not in node:
            raise SchemaError(f"{path}: array requires 'items'")
        _check_node(node["items"], f"{path}[]", depth + 1)
    elif "items" in node:
        raise SchemaError(f"{path}: 'items' is only valid on array types")


def validate_schema(schema: dict[str, Any]) -> None:
    """Raise :class:`SchemaError` unless ``schema`` is an object schema in the subset."""
    _check_node(schema, "$", 0)
    if schema.get("type") != "object":
        raise SchemaError("$: root schema must be of type 'object'")


def _type_matches(value: Any, node_type: str) -> bool:
    if node_type == "object":
        return isinstance(value, dict)
    if node_type == "array":
        return isinstance(value, list)
    if node_type == "string":
        return isinstance(value, str)
    if node_type == "boolean":
        return isinstance(value, bool)
    if node_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if node_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return False


def _collect(value: Any, node: dict[str, Any], path: str, errors: list[str]) -> None:
    node_type = node["type"]
    if not _type_matches(value, node_type):
        errors.append(f"{path}: expected {node_type}, got {type(value).__name__}")
        return
    if "enum" in node and value not in node["enum"]:
        errors.append(f"{path}: value {value!r} is not one of {node['enum']}")
    if node_type == "object":
        properties: dict[str, Any] = node["properties"]
        for name in node.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required property {name!r}")
        for name in value:
            if name not in properties:
                errors.append(f"{path}: unexpected property {name!r}")
        for name, child in properties.items():
            if name in value:
                _collect(value[name], child, f"{path}.{name}", errors)
    elif node_type == "array":
        for index, item in enumerate(value):
            _collect(item, node["items"], f"{path}[{index}]", errors)


def validate_output(value: Any, schema: dict[str, Any]) -> list[str]:
    """Return validation errors for ``value`` against a subset schema (empty if valid).

    The schema must already have passed :func:`validate_schema`.
    """
    errors: list[str] = []
    _collect(value, schema, "$", errors)
    return errors
