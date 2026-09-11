"""CS-033: result schema validator and generator."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from callswarm.calls.schema import (
    BOOLEAN_MESSAGE,
    ResultSchemaInvalid,
    compile_schema,
    generate_result_schema,
    result_schema_errors,
    validate_result_against_schema,
    validate_result_schema,
)
from callswarm.llm import FakeLLMProvider
from callswarm.llm.prompt import BEGIN_FENCE
from callswarm.models import Importance, InformationGap
from tests.conftest import make_intent

# The worked example from 04_CALL_E_INTEGRATION.md, verbatim in structure.
WORKED_EXAMPLE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["date_available", "quoted_price_status", "jain_option_available"],
    "properties": {
        "date_available": {
            "type": "string",
            "enum": ["yes", "no", "unknown"],
            "description": (
                "Whether the venue is free on the requested date. Use unknown if staff could "
                "not confirm."
            ),
        },
        "quoted_price_status": {
            "type": "string",
            "enum": ["quoted", "refused", "unknown"],
            "description": (
                "Whether staff gave a concrete total price. Use refused if they declined to "
                "quote over the phone, unknown if the call did not establish it."
            ),
        },
        "quoted_price_inr": {
            "type": "number",
            "description": (
                "Total quoted package price in INR for the stated guest count. Present only "
                "when quoted_price_status is quoted."
            ),
        },
        "jain_option_available": {
            "type": "string",
            "enum": ["yes", "no", "unknown"],
            "description": "Whether Jain-compliant catering can be provided.",
        },
        "decoration_included": {
            "type": "string",
            "enum": ["yes", "no", "unknown"],
            "description": "Whether decoration is included in the quoted price.",
        },
        "evidence_summary": {
            "type": "string",
            "description": (
                "One sentence quoting what staff actually said to support the answers above."
            ),
        },
    },
}


def good() -> dict[str, Any]:
    return copy.deepcopy(WORKED_EXAMPLE)


def test_worked_example_passes() -> None:
    validate_result_schema(WORKED_EXAMPLE)
    assert result_schema_errors(WORKED_EXAMPLE) == []


def test_nested_object_and_array_are_accepted() -> None:
    schema = good()
    schema["properties"]["slots"] = {
        "type": "array",
        "description": "Offered time slots.",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["slot_status"],
            "properties": {
                "slot_status": {
                    "type": "string",
                    "enum": ["offered", "unknown"],
                    "description": "x",
                },
                "label": {"type": "string", "description": "As stated."},
            },
        },
    }
    validate_result_schema(schema)


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda s: s["properties"].__setitem__("x", {"$ref": "#/properties/y"}), "$ref"),
        (
            lambda s: s["properties"].__setitem__(
                "x", {"oneOf": [{"type": "string"}, {"type": "number"}]}
            ),
            "oneOf",
        ),
        (lambda s: s["properties"].__setitem__("x", {"anyOf": [{"type": "string"}]}), "anyOf"),
        (lambda s: s["properties"].__setitem__("x", {"allOf": [{"type": "string"}]}), "allOf"),
        (lambda s: s.__setitem__("additionalProperties", True), "additionalProperties"),
        (
            lambda s: s["properties"].__setitem__(
                "x", {"type": "string", "format": "date", "description": "d"}
            ),
            "format",
        ),
        (lambda s: s.__setitem__("type", "array"), "object"),
        (lambda s: s.pop("additionalProperties"), "additionalProperties"),
    ],
    ids=["ref", "oneOf", "anyOf", "allOf", "additional_true", "format", "root_type", "no_ap"],
)
def test_each_forbidden_feature_is_rejected_individually(mutate: Any, fragment: str) -> None:
    schema = good()
    mutate(schema)
    with pytest.raises(ResultSchemaInvalid) as info:
        validate_result_schema(schema)
    assert fragment in str(info.value)


def test_deep_recursion_is_rejected() -> None:
    node: dict[str, Any] = {"type": "string", "description": "leaf"}
    for _ in range(8):
        node = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"inner": node},
        }
    with pytest.raises(ResultSchemaInvalid, match="nesting deeper"):
        validate_result_schema(node)


def test_boolean_field_is_rejected_with_guidance() -> None:
    schema = good()
    schema["properties"]["confirmed"] = {"type": "boolean", "description": "Confirmed?"}
    with pytest.raises(ResultSchemaInvalid, match=BOOLEAN_MESSAGE):
        validate_result_schema(schema)


def test_enum_without_unknown_is_rejected() -> None:
    schema = good()
    schema["properties"]["date_available"]["enum"] = ["yes", "no"]
    with pytest.raises(ResultSchemaInvalid, match="must contain 'unknown'"):
        validate_result_schema(schema)


@pytest.mark.parametrize("phrase", ["omit if not stated", "if available", "optional", "when given"])
def test_required_but_omittable_is_rejected(phrase: str) -> None:
    schema = good()
    schema["required"].append("quoted_price_inr")
    schema["properties"]["quoted_price_inr"]["description"] = f"Price; {phrase}."
    with pytest.raises(ResultSchemaInvalid, match="omittable"):
        validate_result_schema(schema)


def test_optional_omittable_numeric_paired_with_status_enum_is_fine() -> None:
    schema = good()
    schema["properties"]["quoted_price_inr"]["description"] = "Price, if available."
    validate_result_schema(schema)


@pytest.mark.parametrize("name", ["summary", "status", "transcript", "call_id", "answered_at"])
def test_reserved_names_are_rejected(name: str) -> None:
    schema = good()
    schema["properties"][name] = {"type": "string", "description": "x"}
    with pytest.raises(ResultSchemaInvalid, match="reserved field name"):
        validate_result_schema(schema)


def test_result_validation_against_schema() -> None:
    ok = {
        "date_available": "yes",
        "quoted_price_status": "quoted",
        "quoted_price_inr": 1000,
        "jain_option_available": "unknown",
    }
    assert validate_result_against_schema(ok, WORKED_EXAMPLE) == []
    bad = {"date_available": "perhaps", "quoted_price_status": "quoted", "extra": 1}
    errors = validate_result_against_schema(bad, WORKED_EXAMPLE)
    assert any("not one of" in e for e in errors)
    assert any("unexpected property 'extra'" in e for e in errors)
    assert any("missing required property 'jain_option_available'" in e for e in errors)


# --- generation ---------------------------------------------------------------------


def _gaps(mission_id: str) -> list[InformationGap]:
    return [
        InformationGap(
            mission_id=mission_id,
            question="Is the requested date open?",
            importance=Importance.HIGH,
            possible_resolution_methods=["call"],
        )
    ]


GOOD_PROPOSAL = {
    "fields": [
        {
            "name": "date_open",
            "kind": "enum",
            "enum_values": ["yes", "no", "unknown"],
            "description": "Whether the requested date is open.",
            "required": True,
        },
        {
            "name": "price_status",
            "kind": "enum",
            "enum_values": ["quoted", "refused", "unknown"],
            "description": "Whether a price was given.",
            "required": True,
        },
        {
            "name": "price_amount",
            "kind": "number",
            "description": "Amount stated. Present only when price_status is quoted.",
            "required": False,
        },
        {
            "name": "stated_quote",
            "kind": "string",
            "description": "What the recipient said.",
            "required": False,
        },
    ]
}

BAD_PROPOSAL = {
    "fields": [
        {
            "name": "date_open",
            "kind": "enum",
            "enum_values": ["yes", "no"],
            "description": "Missing unknown.",
            "required": True,
        },
        {"name": "status", "kind": "string", "description": "reserved", "required": False},
    ]
}


async def test_generation_compiles_and_validates_and_fences_inputs() -> None:
    llm = FakeLLMProvider([GOOD_PROPOSAL])
    intent = make_intent("m")
    schema = await generate_result_schema(intent, _gaps("m"), llm)
    validate_result_schema(schema)
    assert schema["required"] == ["date_open", "price_status"]
    assert schema["properties"]["date_open"]["enum"] == ["yes", "no", "unknown"]
    assert schema["properties"]["price_amount"]["type"] == "number"
    recorded = llm.calls[0]
    assert BEGIN_FENCE in recorded.inputs["call_intent"]
    assert BEGIN_FENCE in recorded.inputs["information_gaps"]


async def test_generation_retries_once_with_errors_then_raises() -> None:
    llm = FakeLLMProvider([BAD_PROPOSAL, BAD_PROPOSAL])
    with pytest.raises(ResultSchemaInvalid) as info:
        await generate_result_schema(make_intent("m"), _gaps("m"), llm)
    assert info.value.attempts == 2
    assert any("unknown" in e for e in info.value.errors)
    assert any("reserved" in e for e in info.value.errors)
    assert len(llm.calls) == 2
    assert "validator_errors" in llm.calls[1].inputs
    assert "reserved" in llm.calls[1].inputs["validator_errors"]


async def test_generation_succeeds_on_the_retry() -> None:
    llm = FakeLLMProvider([BAD_PROPOSAL, GOOD_PROPOSAL])
    schema = await generate_result_schema(make_intent("m"), _gaps("m"), llm)
    assert "date_open" in schema["properties"]
    assert len(llm.calls) == 2


def test_compile_never_emits_forbidden_keys() -> None:
    from callswarm.calls.schema import ResultSchemaProposal

    schema = compile_schema(ResultSchemaProposal.model_validate(GOOD_PROPOSAL))
    assert set(schema) == {"type", "additionalProperties", "required", "properties"}
    assert schema["additionalProperties"] is False


def test_list_type_and_non_string_enum_are_rejected_cleanly() -> None:
    schema = good()
    schema["properties"]["x"] = {"type": ["boolean", "null"], "description": "d"}
    with pytest.raises(ResultSchemaInvalid, match="'type' must be a single string"):
        validate_result_schema(schema)
    schema = good()
    schema["properties"]["x"] = {"type": "boolean", "enum": [True, False], "description": "d"}
    with pytest.raises(ResultSchemaInvalid, match="string types only"):
        validate_result_schema(schema)
