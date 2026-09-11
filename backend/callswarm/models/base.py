"""Base model and shared helpers for domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

JsonValue = str | int | float | bool | None | dict[str, Any] | list[Any]


def new_id() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class DomainModel(BaseModel):
    """Strict, JSON-round-trippable base for every domain model."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)


class IdentifiedModel(DomainModel):
    """A domain model with a generated string primary key."""

    id: str = Field(default_factory=new_id)
