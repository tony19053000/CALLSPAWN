"""CalleProvider: the real CALL-E Developer API provider (CS-032).

Base URL re-confirmation (2026-09-11)
-------------------------------------
The vendored OpenAPI snapshot (``docs/vendor/calle.openapi.yaml``, version
0.7.0) labels ``https://api.heycall-e.com`` a *placeholder*, so it was
re-checked against three live sources before this module was written:

* ``https://docs.heycall-e.com/quickstart`` — names ``CALLE_API_KEY``, the
  ``calle-ai`` package and ``from calle import CalleClient``; it does not
  print a base URL.
* ``https://github.com/CALLE-AI/call-e-integrations`` README (raw ``main``) —
  states "use ``https://api.heycall-e.com`` as the base URL with bearer token
  authentication".
* ``calle-ai`` 0.7.0 on PyPI (``calle/client.py``) — ``CalleClient.__init__``
  defaults ``base_url="https://api.heycall-e.com"`` and sends
  ``Authorization: Bearer <api_key>`` on an ``httpx.Client``; ``calle/calls.py``
  posts ``/v1/calls`` with an ``Idempotency-Key`` header and treats
  ``completed | failed | canceled`` as terminal.

All three agree, so the ``CALLE_API_BASE_URL`` setting keeps that default and
remains the single source of truth; nothing here hardcodes a host. The SDK's
``calle/webhooks.py`` also confirms that current CALL-E webhooks are
**unsigned** (its HMAC helpers are marked deprecated), which is why the
receiver in ``api/webhooks.py`` relies on a shared-secret path token.

The SDK is synchronous (``httpx.Client``); CallSwarm's backend is async and
needs control over idempotency, reconciliation and masking, so this module
talks to the same three endpoints with an ``httpx.AsyncClient`` and mirrors
the SDK's request shape rather than importing it.

Contract facts encoded here
---------------------------
* ``POST /v1/calls`` is never retried. A timeout, transport failure or 5xx on
  the create raises :class:`AmbiguousCreate`; the service resolves that by
  :meth:`CalleProvider.reconcile`, a **byte-identical replay under the same
  ``Idempotency-Key``**, which CALL-E answers with the original call task. If
  the intent changed in between so the body would differ, reconcile refuses
  rather than dial.
* The phone number is never in the prose ``task``; it travels only in the
  structured ``recipients[]`` so the authorization gate has a structured
  target. ``task`` is built by code from the intent's goal, purpose and the
  fields the result schema asks for.
* ``structured_result: null`` is an explicit unresolved outcome: gaps stay
  ``UNKNOWN``; nothing here treats it as an error.
* Status values are mapped one-to-one onto the exact CALL-E enums.
* Every log line passes through a phone-masking filter and the request body
  is never logged.
* There is no remote cancel; ``cancel_local`` only blocks an unexecuted
  intent and cancels unfired jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from callswarm.calls.provider import (
    NON_TERMINAL_STATUSES,
    AuthorizedPlan,
    CallEvent,
    CallGateProtocol,
    CallPlan,
    CallProviderError,
    CallProviderNotAvailable,
    CallResult,
    EventPage,
    GatedCallProvider,
    IntentAlreadyExecuted,
    cancel_intent_locally,
    idempotency_key,
)
from callswarm.calls.schema import validate_result_schema
from callswarm.config.settings import Settings
from callswarm.models import (
    AttemptStatus,
    CallIntent,
    CallRun,
    CallStatus,
    CompletionConfidence,
    RecipientResult,
    RecipientStatus,
)
from callswarm.persistence import CallIntentRepository, CallRunRepository, Database
from callswarm.sanitize import contains_phone, mask_phone, mask_phones, replace_phones

logger = logging.getLogger(__name__)

CREATE_PATH = "/v1/calls"
RECIPIENT_PLACEHOLDER = "the listed recipient"
MISSION_ID_KEY = "mission_id"
CALL_INTENT_ID_KEY = "call_intent_id"
MAX_EVENT_PAGES = 1_000
EVENTS_PAGE_LIMIT_MAX = 100
BACKOFF_MULTIPLIER = 2.0
BACKOFF_MAX_FACTOR = 8.0


class _PhoneMaskingFilter(logging.Filter):
    """Masks anything phone-shaped in every record this module logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_phones(record.getMessage())
        record.args = ()
        return True


logger.addFilter(_PhoneMaskingFilter())


# --- errors ----------------------------------------------------------------------------


class CalleAPIError(CallProviderError):
    """A non-success HTTP response from CALL-E, typed by ``kind``.

    ``kind`` is ``auth`` (401/403), ``rate_limited`` (429, with
    ``retry_after`` seconds when the header was present), ``not_found`` (404),
    ``server_error`` (5xx outside the create path) or the spec's error
    ``code`` for any other 4xx. The message is the envelope's message.
    """

    def __init__(
        self,
        kind: str,
        *,
        status_code: int,
        code: str,
        message: str,
        retry_after: float | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"{kind}: {code}: {mask_phones(message)}")


class AmbiguousCreate(CallProviderError):
    """``POST /v1/calls`` did not return a definitive answer (timeout,
    transport failure or 5xx). CALL-E may or may not have accepted the task.
    Resolved only by :meth:`CalleProvider.reconcile`; never by a fresh POST."""

    def __init__(self, key: str, reason: str) -> None:
        self.idempotency_key = key
        self.reason = reason
        super().__init__(f"ambiguous create ({reason}); reconcile by identical replay")


class CallPollTimeout(CallProviderError):
    """``wait_for_terminal`` gave up; the call may still be running remotely."""


# --- response models (lenient on unknown fields, strict on what we consume) --------


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ConfidenceResponse(_Lenient):
    score: float = Field(ge=0.0, le=1.0)
    label: str = ""


class AttemptResponse(_Lenient):
    id: str
    phone: str = ""
    status: AttemptStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    summary: str | None = None
    transcript_turns: list[dict[str, Any]] = Field(default_factory=list)
    provider_call_id: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None


class RecipientResponse(_Lenient):
    id: str
    phones: list[str] = Field(default_factory=list)
    locale: str | None = None
    region: str | None = None
    status: RecipientStatus
    structured_result: dict[str, Any] | None = None
    summary: str | None = None
    attempts: list[AttemptResponse] = Field(default_factory=list)


class CallTaskResponse(_Lenient):
    """The spec's ``CallTask``. ``status`` must be one of the exact enum values."""

    id: str
    status: CallStatus
    task: str = ""
    recipients: list[RecipientResponse] = Field(default_factory=list)
    structured_result: dict[str, Any] | None = None
    summary: str | None = None
    task_completed: bool | None = None
    completion_confidence: ConfidenceResponse | None = None
    evidence: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class DeveloperEventResponse(_Lenient):
    id: str
    type: str
    call_id: str
    created_at: datetime
    level: str = "info"
    status: CallStatus | None = None
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class EventListResponse(_Lenient):
    data: list[DeveloperEventResponse] = Field(default_factory=list)
    next_cursor: str | None = None


@dataclass(frozen=True)
class _AttemptedCreate:
    """What was sent under a key, kept so a replay can be proven identical."""

    key: str
    body: bytes


# --- request construction (pure) ------------------------------------------------------


def _fields_to_collect(schema: dict[str, Any] | None) -> list[str]:
    if not schema:
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    lines: list[str] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        description = str(spec.get("description", "")).strip()
        enum = spec.get("enum")
        choice = (
            f" (one of: {', '.join(str(v) for v in enum)})"
            if isinstance(enum, list) and enum
            else ""
        )
        lines.append(f"- {name}{choice}: {description}" if description else f"- {name}{choice}")
    return lines


def build_task_text(intent: CallIntent) -> str:
    """Compose the natural-language ``task`` from the intent, by code.

    The recipient's number never appears: CALL-E receives it in
    ``recipients[]``. Any number the model wrote into the goal, purpose or a
    field description is replaced with a neutral placeholder.
    """
    parts: list[str] = [intent.call_goal.strip()]
    if intent.purpose.strip():
        parts.append(f"Purpose of this call: {intent.purpose.strip()}")
    task_fields = _fields_to_collect(intent.result_schema or None)
    if task_fields:
        parts.append("Information to collect:\n" + "\n".join(task_fields))
    recipient_fields = _fields_to_collect(intent.recipient_result_schema)
    if recipient_fields:
        parts.append("For each recipient, also establish:\n" + "\n".join(recipient_fields))
    parts.append(
        "If a point cannot be established on the call, say so plainly rather than guessing."
    )
    text = replace_phones("\n\n".join(parts), RECIPIENT_PLACEHOLDER)
    assert not contains_phone(text)
    return text


def build_create_request(plan: CallPlan, webhook_url: str | None) -> dict[str, Any]:
    """The ``CreateCallRequest`` body for a plan. Optional keys absent when unset,
    matching the SDK; ``recipients[]`` always explicit with ``phones``, ``region``
    and ``locale`` present (``null`` when unknown)."""
    body: dict[str, Any] = {
        "task": plan.task,
        "recipients": [
            {"phones": [r.phone_e164], "region": r.region, "locale": r.locale}
            for r in plan.recipients
        ],
        "metadata": {
            MISSION_ID_KEY: plan.mission_id,
            CALL_INTENT_ID_KEY: plan.call_intent_id,
        },
    }
    if plan.result_schema is not None:
        body["result_schema"] = plan.result_schema
    if plan.recipient_result_schema is not None:
        body["recipient_result_schema"] = plan.recipient_result_schema
    if webhook_url:
        body["webhook_url"] = webhook_url
    return body


def encode_request(body: dict[str, Any]) -> bytes:
    """Canonical bytes: sorted keys, compact separators. A replay of the same
    plan is byte-identical, which is what the idempotency contract requires."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


# --- response mapping (pure) ---------------------------------------------------------


def _recipient_ref(index: int, response: RecipientResponse, intent: CallIntent | None) -> str:
    """Stable ref shared with the fake provider: the candidate id when the
    CALL-E recipient's number matches an intent recipient, else by index."""
    if intent is not None:
        by_phone = {r.phone_e164: r for r in intent.recipients}
        for phone in response.phones:
            match = by_phone.get(phone)
            if match is not None:
                return match.entity_id or f"recipient-{intent.recipients.index(match)}"
        if index < len(intent.recipients):
            return intent.recipients[index].entity_id or f"recipient-{index}"
    return response.id


def _transcript_reference(task: CallTaskResponse) -> str | None:
    refs = [
        f"calle:{task.id}/attempt:{attempt.id}"
        for recipient in task.recipients
        for attempt in recipient.attempts
        if attempt.transcript_turns
    ]
    return ";".join(refs) if refs else None


def _started_at(task: CallTaskResponse) -> datetime | None:
    starts = [
        attempt.started_at
        for recipient in task.recipients
        for attempt in recipient.attempts
        if attempt.started_at is not None
    ]
    return min(starts) if starts else task.created_at


def task_to_run(
    task: CallTaskResponse,
    intent: CallIntent | None,
    *,
    mission_id: str | None = None,
    call_intent_id: str | None = None,
) -> CallRun:
    """Map a ``CallTask`` onto ``CallRun``. Correlation ids come from the intent
    when known, else from the metadata CallSwarm wrote on create."""
    mission = (
        mission_id or (intent.mission_id if intent else None) or task.metadata.get(MISSION_ID_KEY)
    )
    intent_id = (
        call_intent_id or (intent.id if intent else None) or task.metadata.get(CALL_INTENT_ID_KEY)
    )
    if not isinstance(mission, str) or not isinstance(intent_id, str):
        raise CallProviderError(
            f"call task {task.id!r} carries no CallSwarm correlation metadata; not ours"
        )
    recipient_results = [
        RecipientResult(
            recipient_ref=_recipient_ref(index, recipient, intent),
            phone_masked=", ".join(mask_phone(p) for p in recipient.phones if contains_phone(p)),
            status=recipient.status,
            structured_result=recipient.structured_result,
            summary=mask_phones(recipient.summary or ""),
        )
        for index, recipient in enumerate(task.recipients)
    ]
    masked = ", ".join(
        mask_phone(p) for r in task.recipients for p in r.phones if contains_phone(p)
    )
    if not masked and intent is not None:
        masked = ", ".join(mask_phone(r.phone_e164) for r in intent.recipients)
    return CallRun(
        mission_id=mission,
        call_intent_id=intent_id,
        calle_call_id=task.id,
        status=task.status,
        recipient_masked=masked,
        started_at=_started_at(task),
        completed_at=task.completed_at,
        structured_result=task.structured_result,
        summary=mask_phones(task.summary or ""),
        task_completed=task.task_completed,
        recipient_results=recipient_results,
        transcript_reference=_transcript_reference(task),
        evidence=[mask_phones(item) for item in task.evidence],
        confidence=(
            None
            if task.completion_confidence is None
            else CompletionConfidence(
                score=task.completion_confidence.score, label=task.completion_confidence.label
            )
        ),
        failure_code=task.failure_code,
        failure_message=None if task.failure_message is None else mask_phones(task.failure_message),
        is_simulated=False,
    )


def run_to_result(run: CallRun) -> CallResult:
    assert run.calle_call_id is not None
    return CallResult(
        calle_call_id=run.calle_call_id,
        status=run.status,
        structured_result=run.structured_result,
        summary=run.summary,
        evidence=list(run.evidence),
        confidence=run.confidence,
        task_completed=run.task_completed,
        failure_code=run.failure_code,
        failure_message=run.failure_message,
        recipient_results=list(run.recipient_results),
        is_simulated=False,
    )


def _event_to_model(event: DeveloperEventResponse) -> CallEvent:
    return CallEvent(
        id=event.id,
        calle_call_id=event.call_id,
        event_type=event.type,
        created_at=event.created_at,
        data={
            "level": event.level,
            "status": None if event.status is None else event.status.value,
            "message": mask_phones(event.message),
            "details": _mask_values(event.details),
        },
    )


def _mask_values(value: Any) -> Any:
    if isinstance(value, str):
        return mask_phones(value)
    if isinstance(value, dict):
        return {str(k): _mask_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_values(v) for v in value]
    return value


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def api_error_from_response(response: httpx.Response) -> CalleAPIError:
    """Map an error response to a typed error using the spec's error envelope."""
    code = "internal_error"
    message = "CALL-E API request failed"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        if isinstance(error.get("code"), str):
            code = error["code"]
        if isinstance(error.get("message"), str):
            message = error["message"]
    status = response.status_code
    if status in (401, 403):
        kind = "auth"
    elif status == 429:
        kind = "rate_limited"
    elif status == 404:
        kind = "not_found"
    elif status >= 500:
        kind = "server_error"
    else:
        kind = code
    return CalleAPIError(
        kind,
        status_code=status,
        code=code,
        message=message,
        retry_after=_parse_retry_after(response) if status == 429 else None,
    )


# --- provider ----------------------------------------------------------------------------


SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]


class CalleProvider(GatedCallProvider):
    """Real provider. ``execute`` is inherited and final: the gate runs first."""

    name = "calle"
    is_simulated = False

    def __init__(
        self,
        gate: CallGateProtocol,
        settings: Settings,
        database: Database,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn = asyncio.sleep,
        clock: ClockFn = time.monotonic,
    ) -> None:
        super().__init__(gate, settings, database)
        if not settings.calle_configured:
            raise CallProviderNotAvailable(
                "CALL_PROVIDER=calle requires CALLE_API_KEY; refusing to start without it "
                "rather than substituting the fake provider"
            )
        assert settings.calle_api_key is not None
        self.base_url = settings.calle_api_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {settings.calle_api_key.get_secret_value()}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(settings.calle_request_timeout_seconds),
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock
        # Populated only inside ``_execute_authorized`` (i.e. after the gate).
        self._attempted: dict[str, _AttemptedCreate] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- plan ----------------------------------------------------------------------
    async def plan_call(self, intent: CallIntent) -> CallPlan:
        schema = intent.result_schema or None
        if schema is not None:
            validate_result_schema(schema)
        recipient_schema = intent.recipient_result_schema or None
        if recipient_schema is not None:
            validate_result_schema(recipient_schema)
        return CallPlan(
            mission_id=intent.mission_id,
            call_intent_id=intent.id,
            task=build_task_text(intent),
            recipients=list(intent.recipients),
            result_schema=schema,
            recipient_result_schema=recipient_schema,
            metadata={MISSION_ID_KEY: intent.mission_id, CALL_INTENT_ID_KEY: intent.id},
            idempotency_key=idempotency_key(intent),
            estimated_cost_units=len(intent.recipients),
        )

    def request_bytes(self, plan: CallPlan) -> bytes:
        return encode_request(build_create_request(plan, self._settings.calle_webhook_url))

    # --- create ----------------------------------------------------------------------
    async def _execute_authorized(self, plan: AuthorizedPlan, intent: CallIntent) -> CallRun:
        key = plan.plan.idempotency_key
        body = self.request_bytes(plan.plan)
        self._attempted[intent.id] = _AttemptedCreate(key=key, body=body)
        logger.info(
            "creating call for intent %s (%d recipient(s), key %s)",
            intent.id,
            len(plan.plan.recipients),
            key,
        )
        task = await self._create(key, body)
        return await self._settle(task_to_run(task, intent), intent)

    async def _create(self, key: str, body: bytes) -> CallTaskResponse:
        """One ``POST /v1/calls``. No retry of any kind lives here."""
        try:
            response = await self._client.post(
                CREATE_PATH,
                content=body,
                headers={"Idempotency-Key": key, "Content-Type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            logger.warning("create timed out under key %s", key)
            raise AmbiguousCreate(key, "timeout") from exc
        except httpx.TransportError as exc:
            logger.warning("create transport failure under key %s: %s", key, type(exc).__name__)
            raise AmbiguousCreate(key, f"transport: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            logger.warning("create returned %d under key %s", response.status_code, key)
            raise AmbiguousCreate(key, f"http {response.status_code}")
        if response.status_code in (200, 201):
            return self._parse_task(response)
        error = api_error_from_response(response)
        logger.warning("create rejected: %s (%d)", error.kind, error.status_code)
        raise error

    async def _settle(self, run: CallRun, intent: CallIntent | None) -> CallRun:
        """With a webhook configured the terminal state arrives by webhook and
        the queued run is returned as-is; without one, poll to terminal."""
        if run.status not in NON_TERMINAL_STATUSES or self._settings.calle_webhook_url:
            return run
        assert run.calle_call_id is not None
        return await self.wait_for_terminal(run.calle_call_id, intent=intent)

    # --- reads --------------------------------------------------------------------------
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise CallProviderError(f"network: timeout on GET {path}") from exc
        except httpx.TransportError as exc:
            raise CallProviderError(f"network: {type(exc).__name__} on GET {path}") from exc
        if response.status_code == 200:
            return response
        error = api_error_from_response(response)
        logger.warning("GET %s failed: %s (%d)", path, error.kind, error.status_code)
        raise error

    @staticmethod
    def _parse_task(response: httpx.Response) -> CallTaskResponse:
        try:
            return CallTaskResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise CallProviderError(
                f"CALL-E returned a call task that does not match the contract: "
                f"{type(exc).__name__}"
            ) from exc

    async def _intent_for(self, task: CallTaskResponse) -> CallIntent | None:
        intent_id = task.metadata.get(CALL_INTENT_ID_KEY)
        if not isinstance(intent_id, str):
            return None
        async with self._database.session() as session:
            return await CallIntentRepository(session).get(intent_id)

    async def get_status(self, calle_call_id: str) -> CallRun:
        response = await self._get(f"{CREATE_PATH}/{calle_call_id}")
        task = self._parse_task(response)
        return task_to_run(task, await self._intent_for(task))

    async def get_events(
        self, calle_call_id: str, cursor: str | None = None, limit: int = 50
    ) -> EventPage:
        """One page. Use :meth:`get_all_events` to follow ``next_cursor``."""
        params: dict[str, Any] = {"limit": max(1, min(limit, EVENTS_PAGE_LIMIT_MAX))}
        if cursor:
            params["cursor"] = cursor
        response = await self._get(f"{CREATE_PATH}/{calle_call_id}/events", params)
        try:
            page = EventListResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise CallProviderError("CALL-E returned an event page off contract") from exc
        return EventPage(
            events=[_event_to_model(e) for e in page.data], next_cursor=page.next_cursor
        )

    async def get_all_events(self, calle_call_id: str, limit: int = 50) -> list[CallEvent]:
        """Every event, following ``next_cursor`` until it is ``null``."""
        events: list[CallEvent] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_EVENT_PAGES):
            page = await self.get_events(calle_call_id, cursor=cursor, limit=limit)
            events.extend(page.events)
            if page.next_cursor is None:
                return events
            if page.next_cursor in seen:
                raise CallProviderError("CALL-E event pagination returned a repeated cursor")
            seen.add(page.next_cursor)
            cursor = page.next_cursor
        raise CallProviderError("CALL-E event pagination exceeded the page limit")

    async def get_result(self, calle_call_id: str) -> CallResult:
        return run_to_result(await self.get_status(calle_call_id))

    async def wait_for_terminal(
        self,
        calle_call_id: str,
        poll_interval: float | None = None,
        timeout: float | None = None,
        *,
        intent: CallIntent | None = None,
    ) -> CallRun:
        """Poll ``GET /v1/calls/{id}`` with exponential backoff (capped at eight
        times the base interval) until the status leaves ``queued``/``in_progress``.
        Raises :class:`CallPollTimeout` after ``timeout`` seconds."""
        base = poll_interval or self._settings.calle_poll_interval_seconds
        limit = timeout or self._settings.calle_poll_timeout_seconds
        deadline = self._clock() + limit
        delay = base
        while True:
            run = await self.get_status(calle_call_id)
            if run.status not in NON_TERMINAL_STATUSES:
                return run
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise CallPollTimeout(
                    f"call {calle_call_id} still {run.status.value} after {limit:g}s"
                )
            await self._sleep(min(delay, remaining))
            delay = min(delay * BACKOFF_MULTIPLIER, base * BACKOFF_MAX_FACTOR)

    # --- reconcile and cancel -------------------------------------------------------
    async def reconcile(self, intent: CallIntent) -> CallRun:
        """Resolve an ambiguous create.

        1. A persisted run already holds a call id: re-read it (authoritative).
        2. A create was attempted in this process under a key: replay the
           **identical bytes under the identical key**. If the intent changed so
           the body or key would differ, refuse; a replay of a different request
           is a new dial.
        3. Nothing was attempted: refuse. Reconcile never starts a call.
        """
        async with self._database.session() as session:
            runs = await CallRunRepository(session).list_by_intent(intent.id)
        for run in runs:
            if run.calle_call_id and not run.is_simulated:
                return await self.get_status(run.calle_call_id)
        attempted = self._attempted.get(intent.id)
        if attempted is None:
            raise CallProviderError(
                f"no create was attempted for intent {intent.id!r} in this process; "
                "reconcile replays a create that was attempted, it never starts one"
            )
        plan = await self.plan_call(intent)
        if plan.idempotency_key != attempted.key or self.request_bytes(plan) != attempted.body:
            raise CallProviderError(
                f"intent {intent.id!r} changed since the create was attempted; a replay "
                "would be a different request. Refusing to dial."
            )
        logger.info(
            "reconciling intent %s by identical replay under key %s", intent.id, attempted.key
        )
        task = await self._create(attempted.key, attempted.body)
        return await self._settle(task_to_run(task, intent), intent)

    async def cancel_local(self, intent: CallIntent) -> CallIntent:
        async with self._database.session() as session:
            runs = await CallRunRepository(session).list_by_intent(intent.id)
        if runs or intent.id in self._attempted:
            raise IntentAlreadyExecuted(
                f"intent {intent.id!r} has already executed; CALL-E exposes no cancel"
            )
        return await cancel_intent_locally(self._database, intent)
