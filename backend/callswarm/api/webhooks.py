"""Terminal webhook receiver (CS-036): ``POST /calle/webhook/{token}``.

The CALL-E spec declares the webhook path unauthenticated and its payload
carries a full ``CallTask`` including ``structured_result`` — an unguarded
receiver is a direct evidence-injection path. Current CALL-E webhooks are
unsigned (confirmed against the ``calle-ai`` 0.7.0 SDK, whose HMAC helpers
are marked deprecated), so protection is layered:

1. ``token`` must constant-time-equal ``CALLE_WEBHOOK_SECRET``. Anything else
   is a 404, not a 401, so the route's existence is not confirmed. No secret
   configured means the receiver is off (always 404).
2. The required ``CALL-E-Event-Id`` header (400 when missing) is persisted as
   a :class:`WebhookReceipt` **before any side effect**; a duplicate event id
   is a 200 no-op.
3. The body must validate as ``WebhookEvent`` with a type in
   ``call.completed | call.failed | call.result_validation_failed``.
4. ``data.id`` must match a non-simulated ``CallRun`` this instance created
   and ``data.metadata.mission_id`` / ``call_intent_id`` must correlate to
   that run; otherwise 404.
5. An accepted payload is a *notification only*. The service re-reads the
   authoritative state with ``GET /v1/calls/{id}`` and writes evidence from
   that re-read, never from the payload.

Payload bodies are never logged; log lines carry the event id, call id and
outcome only. Processing that fails after the receipt leaves the receipt with
``outcome=failed`` so a re-delivery of the same event id may try again — the
only case where a duplicate id is not a no-op, and one in which no side
effect has happened yet.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError

from callswarm.api.deps import get_call_service_dep, get_database_dep, get_settings_dep
from callswarm.calls.service import CallService
from callswarm.config.settings import WEBHOOK_PATH_PREFIX, Settings
from callswarm.models import CallStatus, WebhookEventType, WebhookReceipt, utcnow
from callswarm.persistence import CallRunRepository, Database, WebhookReceiptRepository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

EVENT_ID_HEADER = "CALL-E-Event-Id"
OUTCOME_RECEIVED = "received"
OUTCOME_PROCESSED = "processed"
OUTCOME_NOT_TERMINAL = "not_terminal"
OUTCOME_REJECTED_PAYLOAD = "rejected_payload"
OUTCOME_REJECTED_UNCORRELATED = "rejected_uncorrelated"
OUTCOME_FAILED = "failed"


class WebhookCallData(BaseModel):
    """The subset of ``WebhookCallData`` (a ``CallTask``) used for correlation."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(pattern=r"^call_[A-Za-z0-9_-]+$")
    status: CallStatus
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebhookEventPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    type: WebhookEventType
    created_at: datetime
    data: WebhookCallData


class WebhookAck(BaseModel):
    ok: bool = True
    duplicate: bool = False
    outcome: str = OUTCOME_RECEIVED


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not Found")


def token_matches(token: str, settings: Settings) -> bool:
    secret = settings.calle_webhook_secret
    if secret is None or not secret.get_secret_value():
        return False
    return hmac.compare_digest(token.encode("utf-8"), secret.get_secret_value().encode("utf-8"))


async def _persist_receipt(
    database: Database, event_id: str, call_id: str, event_type: str
) -> WebhookReceipt | None:
    """Insert the receipt; ``None`` when the event id was already recorded
    with any outcome other than ``failed`` (a duplicate delivery)."""
    async with database.session() as session:
        repo = WebhookReceiptRepository(session)
        existing = await repo.get_by_event_id(event_id)
        if existing is not None:
            if existing.outcome != OUTCOME_FAILED:
                return None
            return await repo.update(
                existing.model_copy(update={"outcome": OUTCOME_RECEIVED, "received_at": utcnow()})
            )
    try:
        async with database.session() as session:
            return await WebhookReceiptRepository(session).add(
                WebhookReceipt(event_id=event_id, call_id=call_id, event_type=event_type)
            )
    except IntegrityError:
        # A concurrent delivery of the same event id won the insert.
        return None


async def _set_outcome(database: Database, receipt: WebhookReceipt, outcome: str) -> None:
    async with database.session() as session:
        await WebhookReceiptRepository(session).update(
            receipt.model_copy(update={"outcome": outcome})
        )


@router.post(f"{WEBHOOK_PATH_PREFIX}/{{token}}", response_model=WebhookAck)
async def receive_calle_webhook(
    token: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    database: Annotated[Database, Depends(get_database_dep)],
    service: Annotated[CallService, Depends(get_call_service_dep)],
    event_id: Annotated[str | None, Header(alias=EVENT_ID_HEADER)] = None,
) -> WebhookAck:
    if not token_matches(token, settings):
        raise _not_found()
    if event_id is None or not event_id.strip():
        raise HTTPException(status_code=400, detail=f"missing {EVENT_ID_HEADER} header")
    event_id = event_id.strip()

    try:
        raw = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="body is not JSON") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    data = raw.get("data")
    raw_call_id = data.get("id") if isinstance(data, dict) else None
    raw_type = raw.get("type")

    # Receipt first: everything below is a side effect or a rejection.
    receipt = await _persist_receipt(
        database,
        event_id,
        raw_call_id if isinstance(raw_call_id, str) else "",
        raw_type if isinstance(raw_type, str) else "",
    )
    if receipt is None:
        logger.info("webhook event %s already received; no-op", event_id)
        return WebhookAck(duplicate=True, outcome="duplicate")

    try:
        payload = WebhookEventPayload.model_validate(raw)
    except ValidationError as exc:
        await _set_outcome(database, receipt, OUTCOME_REJECTED_PAYLOAD)
        logger.warning("webhook event %s rejected: payload off contract", event_id)
        raise HTTPException(status_code=400, detail="payload does not match WebhookEvent") from exc

    async with database.session() as session:
        run = await CallRunRepository(session).get_by_calle_call_id(payload.data.id)
    metadata = payload.data.metadata
    if (
        run is None
        or run.is_simulated
        or metadata.get("mission_id") != run.mission_id
        or metadata.get("call_intent_id") != run.call_intent_id
    ):
        await _set_outcome(database, receipt, OUTCOME_REJECTED_UNCORRELATED)
        logger.warning(
            "webhook event %s rejected: call %s does not correlate to a local run",
            event_id,
            payload.data.id,
        )
        raise _not_found()

    try:
        result = await service.finalize_run(payload.data.id, event_type=payload.type)
    except Exception:
        await _set_outcome(database, receipt, OUTCOME_FAILED)
        logger.exception("webhook event %s: finalizing call %s failed", event_id, payload.data.id)
        raise
    outcome = OUTCOME_NOT_TERMINAL if result.pending else OUTCOME_PROCESSED
    await _set_outcome(database, receipt, outcome)
    logger.info("webhook event %s for call %s: %s", event_id, payload.data.id, outcome)
    return WebhookAck(outcome=outcome)
