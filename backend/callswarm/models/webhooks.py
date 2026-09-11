"""Webhook receipts: the idempotency record for CALL-E terminal webhooks."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from callswarm.models.base import IdentifiedModel, utcnow


class WebhookReceipt(IdentifiedModel):
    """One row per ``CALL-E-Event-Id``.

    Persisted *before* any side effect of a webhook delivery, so a duplicate
    delivery of the same event id is ignored safely. ``outcome`` is updated
    once processing finishes and records what happened without storing any
    part of the payload body.
    """

    event_id: str = Field(min_length=1, max_length=255)
    received_at: datetime = Field(default_factory=utcnow)
    call_id: str = Field(default="", max_length=128)
    event_type: str = Field(default="", max_length=64)
    outcome: str = Field(default="received", max_length=64)
