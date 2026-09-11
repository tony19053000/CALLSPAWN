"""FakeLLMProvider: scripted, schema-validated responses for tests.

It consumes a queue of canned responses in order and never inspects the
instruction or inputs to decide what to return. That is deliberate: a fake
that branched on the mission would fake the very differentiation the
generalization tests exist to prove.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from callswarm.llm.provider import AgentOutputInvalid, LLMError, TModel


class FakeLLMExhausted(LLMError):
    """The scripted queue is empty."""


@dataclass(frozen=True)
class RecordedCall:
    instruction: str
    inputs: dict[str, str]
    schema_name: str


@dataclass
class FakeLLMProvider:
    """Returns the next canned response, validated against the requested schema."""

    responses: Iterable[BaseModel | dict[str, Any]] = ()
    calls: list[RecordedCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queue: deque[BaseModel | dict[str, Any]] = deque(self.responses)

    def enqueue(self, *responses: BaseModel | dict[str, Any]) -> None:
        self._queue.extend(responses)

    @property
    def remaining(self) -> int:
        return len(self._queue)

    async def generate_structured(
        self, instruction: str, inputs: Mapping[str, str], schema: type[TModel]
    ) -> TModel:
        self.calls.append(RecordedCall(instruction, dict(inputs), schema.__name__))
        if not self._queue:
            raise FakeLLMExhausted(f"no scripted response left for {schema.__name__}")
        item = self._queue.popleft()
        data = item.model_dump(mode="json") if isinstance(item, BaseModel) else item
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            messages = [
                f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}"
                for e in exc.errors(include_url=False, include_input=False)
            ]
            raise AgentOutputInvalid(schema.__name__, messages, 1) from None
