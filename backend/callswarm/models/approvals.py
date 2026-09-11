"""Explicit approval records and suppression (do-not-contact) entries."""

from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import Field

from callswarm.models.base import IdentifiedModel, utcnow
from callswarm.models.enums import ApprovalStatus, ApprovalSubjectType


class Approval(IdentifiedModel):
    """An explicit authorization record. Never inferred from conversation."""

    mission_id: str
    subject_type: ApprovalSubjectType
    subject_id: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    decided_by: str | None = None
    reason: str | None = None


def hash_phone(phone_e164: str) -> str:
    """Stable, non-reversible key for a suppression entry."""
    return hashlib.sha256(phone_e164.strip().encode("utf-8")).hexdigest()


class SuppressionEntry(IdentifiedModel):
    """Do-not-contact record. Mission-independent; never cascade-deleted."""

    phone_hash: str = Field(min_length=1)
    reason: str = ""
    source: str = ""
    scope: str = "GLOBAL"
    created_at: datetime = Field(default_factory=utcnow)
