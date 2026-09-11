"""The LLMProvider protocol and its typed errors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

TModel = TypeVar("TModel", bound=BaseModel)


class LLMError(Exception):
    """Base class for provider errors."""


class AgentOutputInvalid(LLMError):
    """The model failed to produce a schema-valid artifact after one retry.

    Carries only the schema name and the validator's messages, never raw model
    text.
    """

    def __init__(self, schema_name: str, errors: list[str], attempts: int) -> None:
        self.schema_name = schema_name
        self.errors = errors
        self.attempts = attempts
        joined = "; ".join(errors) if errors else "unknown validation error"
        super().__init__(
            f"model output did not satisfy schema {schema_name!r} after {attempts} attempt(s): "
            f"{joined}"
        )


class LLMNotConfigured(LLMError):
    """No credentials for the reasoning provider are present."""


class LLMModelUnavailable(LLMError):
    """The configured model was not found in the provider's model listing."""


@runtime_checkable
class LLMProvider(Protocol):
    """One validated path to the model.

    ``inputs`` are untrusted and are wrapped as labelled data blocks. The
    returned object is always a validated instance of ``schema``.
    """

    async def generate_structured(
        self, instruction: str, inputs: Mapping[str, str], schema: type[TModel]
    ) -> TModel: ...
