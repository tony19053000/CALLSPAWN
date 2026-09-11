"""User-visible activity events and persisted scheduler jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from callswarm.models.base import IdentifiedModel, utcnow
from callswarm.models.enums import ActivityEventType, ScheduledJobStatus


class ActivityEvent(IdentifiedModel):
    """A high-level, real execution event. ``sequence`` is assigned on persistence
    and doubles as the SSE event id."""

    mission_id: str
    sequence: int | None = None
    event_type: ActivityEventType
    summary: str = Field(min_length=1)
    agent_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ScheduledJob(IdentifiedModel):
    mission_id: str
    job_type: str = Field(min_length=1)
    due_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    status: ScheduledJobStatus = ScheduledJobStatus.PENDING
    status_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
