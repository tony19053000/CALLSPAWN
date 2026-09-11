"""CS-032: CalleProvider against a recorded-response mock of the vendored spec.

No sockets: every request goes through ``httpx.MockTransport``. The mock
server mirrors the contract facts that matter — ``Idempotency-Key`` replay
returns the original task, the error envelope shape, cursor pagination — and
records every request so the tests can assert exactly what was sent.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from callswarm.api.app import create_app
from callswarm.approvals import ApprovalService
from callswarm.calls.calle import (
    AmbiguousCreate,
    CalleAPIError,
    CalleProvider,
    CallPollTimeout,
    build_create_request,
    build_task_text,
    encode_request,
    task_to_run,
)
from callswarm.calls.gates import CallGate
from callswarm.calls.provider import (
    AuthorizedPlan,
    CallNotAuthorized,
    CallProviderError,
    CallProviderNotAvailable,
    GatedCallProvider,
    LiveCallsDisabled,
    RecipientNotAllowed,
    idempotency_key,
)
from callswarm.calls.service import CallService, select_call_provider
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.models import (
    ActivityEventType,
    ApprovalStatus,
    AuthorityPolicy,
    CallBudget,
    CallIntent,
    CallRecipient,
    CallStatus,
    EvidenceStatus,
    Mission,
    MissionStatus,
    RecipientStatus,
    SourceType,
    utcnow,
)
from callswarm.orchestrator.state_machine import MissionStateMachine
from callswarm.persistence import (
    ActivityEventRepository,
    CallRunRepository,
    Database,
    EvidenceClaimRepository,
    MissionRepository,
)
from callswarm.sanitize import Sanitizer
from tests.conftest import TEST_PHONE, TEST_PHONE_2, daytime_region, make_intent, persist_intent
from tests.test_calls_gates import approval_in
from tests.test_calls_provider import GOOD_SCHEMA, approved

API_KEY = "test-calle-key-not-real"
CREATED_AT = "2026-06-01T17:00:00Z"


# --- a recorded-response mock of the spec -------------------------------------------


class MockCalle:
    """Serves ``POST /v1/calls``, ``GET /v1/calls/{id}`` and ``/events`` the way
    the vendored contract describes, and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.create_faults: deque[Any] = deque()
        self.status_sequence: deque[str] = deque()
        self.initial_status = "completed"
        self.structured_result: dict[str, Any] | None = {"answer_status": "yes"}
        self.recipient_results: list[dict[str, Any] | None] | None = None
        self.counter = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def creates(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def task_for(self, body: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        call_id = f"call_{self.counter:03d}"
        recipients = []
        for index, recipient in enumerate(body.get("recipients", [])):
            per_recipient = (
                self.recipient_results[index]
                if self.recipient_results is not None and index < len(self.recipient_results)
                else None
            )
            recipients.append(
                {
                    "id": f"rcp_{self.counter}_{index}",
                    "phones": recipient["phones"],
                    "locale": recipient.get("locale"),
                    "region": recipient.get("region"),
                    "status": "completed" if self.initial_status == "completed" else "pending",
                    "structured_result": per_recipient,
                    "summary": f"recipient {index} said something",
                    "attempts": [
                        {
                            "id": f"att_{self.counter}_{index}",
                            "phone": recipient["phones"][0],
                            "status": "completed",
                            "started_at": "2026-06-01T17:00:05Z",
                            "completed_at": "2026-06-01T17:01:00Z",
                            "summary": "attempt summary",
                            "transcript_turns": [
                                {"offset_seconds": 0, "speaker": "bot", "text": "hello"}
                            ],
                            "provider_call_id": f"provider_{self.counter}",
                            "failure_code": None,
                            "failure_message": None,
                        }
                    ],
                }
            )
        terminal = self.initial_status == "completed"
        task = {
            "id": call_id,
            "object": "call_task",
            "status": self.initial_status,
            "task": body["task"],
            "recipients": recipients,
            "structured_result": self.structured_result if terminal else None,
            "summary": f"Summary for {TEST_PHONE} said yes." if terminal else None,
            "task_completed": True if terminal else None,
            "completion_confidence": {"score": 0.92, "label": "high"} if terminal else None,
            "evidence": [f"They said yes to {TEST_PHONE}."] if terminal else [],
            "metadata": body.get("metadata", {}),
            "failure_code": None,
            "failure_message": None,
            "created_at": CREATED_AT,
            "completed_at": "2026-06-01T17:01:00Z" if terminal else None,
        }
        self.tasks[call_id] = task
        self.events[call_id] = [
            {
                "id": f"evt_{self.counter}_{n}",
                "type": f"call.{status}",
                "call_id": call_id,
                "created_at": CREATED_AT,
                "level": "info",
                "status": status,
                "message": f"Call {status} for {TEST_PHONE}",
                "details": {"n": n},
            }
            for n, status in enumerate(("queued", "in_progress", "completed"))
        ]
        return task

    @staticmethod
    def error(status: int, code: str, message: str, **headers: str) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"code": code, "message": message, "details": {}}},
            headers={"Cache-Control": "no-store", **headers},
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v1/calls":
            if self.create_faults:
                fault = self.create_faults.popleft()
                if isinstance(fault, Exception):
                    raise fault
                if isinstance(fault, httpx.Response):
                    return fault
            key = request.headers.get("Idempotency-Key")
            if key and key in self.by_key:
                return httpx.Response(201, json=self.tasks[self.by_key[key]])
            body = json.loads(request.content)
            task = self.task_for(body)
            if key:
                self.by_key[key] = task["id"]
            return httpx.Response(201, json=task)
        if request.method == "GET" and path.startswith("/v1/calls/"):
            parts = path.split("/")
            call_id = parts[3]
            if call_id not in self.tasks:
                return self.error(404, "not_found", "no such call")
            if len(parts) == 5 and parts[4] == "events":
                cursor = request.url.params.get("cursor")
                limit = int(request.url.params.get("limit", "50"))
                start = int(cursor) if cursor else 0
                page = self.events[call_id][start : start + limit]
                end = start + len(page)
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": page,
                        "next_cursor": str(end) if end < len(self.events[call_id]) else None,
                    },
                )
            task = dict(self.tasks[call_id])
            if self.status_sequence:
                task["status"] = self.status_sequence.popleft()
                self.tasks[call_id] = task
            return httpx.Response(200, json=task)
        return self.error(404, "not_found", "unknown route")


# --- fixtures ------------------------------------------------------------------------------


def calle_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "call_provider": "calle",
        "calle_api_key": API_KEY,
        "calle_live_calls_enabled": True,
        "call_allowed_recipients": [TEST_PHONE, TEST_PHONE_2],
        "calle_poll_interval_seconds": 0.01,
        "calle_poll_timeout_seconds": 1.0,
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'calle.db'}",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class Env:
    def __init__(self, settings: Settings, database: Database, mock: MockCalle) -> None:
        self.settings = settings
        self.database = database
        self.mock = mock
        self.emitter = ActivityEventEmitter(database, Sanitizer(settings.reasoning_leak_markers))
        self.gate = CallGate(database, self.emitter)
        self.sleeps: list[float] = []
        self.clock_value = 0.0

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.clock_value += seconds

        self.provider = CalleProvider(
            self.gate,
            settings,
            database,
            transport=mock.transport,
            sleep=sleep,
            clock=lambda: self.clock_value,
        )
        self.machine = MissionStateMachine(database, self.emitter)
        self.approvals = ApprovalService(database, self.emitter, settings, self.machine)
        self.service = CallService(
            self.provider,
            self.gate,
            self.approvals,
            database,
            self.emitter,
            settings,
            self.machine,
        )

    async def mission(self) -> Mission:
        async with self.database.session() as session:
            return await MissionRepository(session).add(
                Mission(
                    user_goal="test goal",
                    status=MissionStatus.CALL_AUTHORIZED,
                    authority_policy=AuthorityPolicy(calls_allowed=True, max_call_count=3),
                    call_budget=CallBudget(max_calls=3),
                )
            )

    async def approved_intent(
        self, mission: Mission, **kwargs: Any
    ) -> tuple[CallIntent, AuthorizedPlan]:
        intent = await persist_intent(
            self.database, make_intent(mission.id, result_schema=GOOD_SCHEMA, **kwargs)
        )
        approval = await approved(self.database, intent)
        plan = await self.provider.plan_call(intent)
        return intent, await self.provider.authorize(plan, approval)

    async def events(self, mission_id: str) -> list[dict[str, Any]]:
        async with self.database.session() as session:
            events = await ActivityEventRepository(session).list_by_mission(mission_id)
        return [{"summary": e.summary, "event_type": e.event_type, **e.payload} for e in events]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    settings = calle_settings(tmp_path)
    database = Database(settings.database_url.get_secret_value())
    await database.create_schema()
    e = Env(settings, database, MockCalle())
    try:
        yield e
    finally:
        await e.provider.aclose()
        await database.dispose()


@pytest.fixture
async def webhook_env(tmp_path: Path) -> AsyncIterator[Env]:
    settings = calle_settings(
        tmp_path, calle_webhook_url="https://example.test/calle/webhook/x", calle_webhook_secret="x"
    )
    database = Database(settings.database_url.get_secret_value())
    await database.create_schema()
    e = Env(settings, database, MockCalle())
    e.mock.initial_status = "queued"
    try:
        yield e
    finally:
        await e.provider.aclose()
        await database.dispose()


# --- construction and startup -----------------------------------------------------------


def test_calle_provider_is_real_and_gated() -> None:
    assert CalleProvider.is_simulated is False
    assert CalleProvider.name == "calle"
    assert CalleProvider.execute is GatedCallProvider.execute
    assert not hasattr(CalleProvider, "cancel"), "no remote cancel exists"
    signature = inspect.signature(CalleProvider.get_events)
    assert list(signature.parameters)[1:] == ["calle_call_id", "cursor", "limit"]


def test_base_url_comes_from_settings_only(env: Env) -> None:
    assert env.provider.base_url == env.settings.calle_api_base_url.rstrip("/")
    source = Path(inspect.getfile(CalleProvider)).read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("*", "#"))
    )
    # The docstring records the verified URL; the code never hardcodes it.
    body = code.split('"""', 2)[2]
    assert "heycall-e.com" not in body


async def test_select_calle_with_key_starts_and_without_key_refuses(
    tmp_path: Path, emitter: ActivityEventEmitter
) -> None:
    with_key = calle_settings(tmp_path)
    db = Database(with_key.database_url.get_secret_value())
    await db.create_schema()
    try:
        provider = select_call_provider(with_key, db, emitter)
        assert isinstance(provider, CalleProvider) and provider.is_simulated is False
        await provider.aclose()
        without = calle_settings(tmp_path, calle_api_key=None)
        with pytest.raises(CallProviderNotAvailable, match="CALLE_API_KEY"):
            select_call_provider(without, db, emitter)
    finally:
        await db.dispose()


async def test_app_starts_with_calle_provider_when_key_present(tmp_path: Path) -> None:
    settings = calle_settings(tmp_path, calle_live_calls_enabled=False)
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        assert isinstance(application.state.call_provider, CalleProvider)
        assert application.state.call_provider.is_simulated is False


# --- request shape -------------------------------------------------------------------------


def test_task_text_never_contains_the_number() -> None:
    intent = make_intent("m", region="US")
    intent = intent.model_copy(
        update={
            "call_goal": f"Call {TEST_PHONE} and ask about availability.",
            "purpose": f"reach {TEST_PHONE_2}",
            "result_schema": GOOD_SCHEMA,
        }
    )
    text = build_task_text(intent)
    assert TEST_PHONE not in text and TEST_PHONE_2 not in text
    assert "the listed recipient" in text
    assert "answer_status (one of: yes, no, unknown)" in text
    assert "quoted_amount" in text


async def test_create_request_matches_the_spec(env: Env) -> None:
    mission = await env.mission()
    intent, authorized = await env.approved_intent(mission)
    run = await env.provider.execute(authorized)

    assert len(env.mock.creates) == 1
    request = env.mock.creates[0]
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["Idempotency-Key"] == idempotency_key(intent)
    assert request.headers["Content-Type"] == "application/json"
    assert request.url.path == "/v1/calls"
    body = json.loads(request.content)
    assert set(body) == {"task", "recipients", "result_schema", "metadata"}
    assert body["recipients"] == [
        {"phones": [TEST_PHONE], "region": intent.recipients[0].region, "locale": None}
    ]
    assert body["metadata"] == {"mission_id": mission.id, "call_intent_id": intent.id}
    assert body["result_schema"] == GOOD_SCHEMA
    assert TEST_PHONE not in body["task"]
    # Byte-identical encoding of the same plan.
    plan = await env.provider.plan_call(intent)
    assert encode_request(build_create_request(plan, None)) == request.content

    assert run.is_simulated is False
    assert run.calle_call_id == "call_001"
    assert run.status is CallStatus.COMPLETED
    assert run.structured_result == {"answer_status": "yes"}
    assert run.mission_id == mission.id and run.call_intent_id == intent.id


async def test_webhook_url_is_sent_when_configured(webhook_env: Env) -> None:
    mission = await webhook_env.mission()
    _, authorized = await webhook_env.approved_intent(mission)
    run = await webhook_env.provider.execute(authorized)
    body = json.loads(webhook_env.mock.creates[0].content)
    assert body["webhook_url"] == "https://example.test/calle/webhook/x"
    # With a webhook configured the queued run is returned, not polled.
    assert run.status is CallStatus.QUEUED
    assert [r.method for r in webhook_env.mock.requests] == ["POST"]


# --- response mapping ---------------------------------------------------------------------


def _task(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "call_x",
        "object": "call_task",
        "status": "completed",
        "task": "t",
        "recipients": [],
        "structured_result": {"answer_status": "no"},
        "summary": "s",
        "task_completed": True,
        "completion_confidence": {"score": 0.5, "label": "medium"},
        "evidence": ["e"],
        "metadata": {"mission_id": "m", "call_intent_id": "i"},
        "failure_code": None,
        "failure_message": None,
        "created_at": CREATED_AT,
        "completed_at": None,
    }
    base.update(overrides)
    return base


def test_status_enum_is_mapped_exactly_and_unknown_is_rejected() -> None:
    from callswarm.calls.calle import CallTaskResponse

    for value in ("queued", "in_progress", "completed", "failed", "canceled"):
        run = task_to_run(CallTaskResponse.model_validate(_task(status=value)), None)
        assert run.status.value == value
    with pytest.raises(Exception, match="status"):
        CallTaskResponse.model_validate(_task(status="done"))


def test_failed_canceled_and_null_result_mapping() -> None:
    from callswarm.calls.calle import CallTaskResponse

    failed = task_to_run(
        CallTaskResponse.model_validate(
            _task(
                status="failed",
                structured_result=None,
                task_completed=False,
                failure_code="no_answer",
                failure_message=f"no answer from {TEST_PHONE}",
            )
        ),
        None,
    )
    assert failed.status is CallStatus.FAILED
    assert failed.failure_code == "no_answer"
    assert failed.failure_message is not None and TEST_PHONE not in failed.failure_message
    canceled = task_to_run(CallTaskResponse.model_validate(_task(status="canceled")), None)
    assert canceled.status is CallStatus.CANCELED
    null = task_to_run(
        CallTaskResponse.model_validate(
            _task(structured_result=None, summary="nothing established", evidence=[])
        ),
        None,
    )
    assert null.status is CallStatus.COMPLETED and null.structured_result is None
    assert null.summary == "nothing established"
    assert null.is_simulated is False


def test_missing_correlation_metadata_is_not_ours() -> None:
    from callswarm.calls.calle import CallTaskResponse

    with pytest.raises(CallProviderError, match="not ours"):
        task_to_run(CallTaskResponse.model_validate(_task(metadata={})), None)


async def test_per_recipient_results_are_mapped_and_masked(env: Env) -> None:
    env.mock.recipient_results = [{"can_attend": "yes"}, {"can_attend": "no"}]
    mission = await env.mission()
    intent = make_intent(mission.id, entity_id="ent-a")
    intent = intent.model_copy(
        update={
            "recipients": [
                CallRecipient(phone_e164=TEST_PHONE, region=daytime_region(), entity_id="ent-a"),
                CallRecipient(phone_e164=TEST_PHONE_2, region=daytime_region(), entity_id="ent-b"),
            ],
            "recipient_result_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["can_attend"],
                "properties": {
                    "can_attend": {
                        "type": "string",
                        "enum": ["yes", "no", "unknown"],
                        "description": "d",
                    }
                },
            },
        }
    )
    intent = await persist_intent(env.database, intent)
    approval = await approved(env.database, intent)
    run = await env.provider.execute(
        await env.provider.authorize(await env.provider.plan_call(intent), approval)
    )
    body = json.loads(env.mock.creates[0].content)
    assert [r["phones"] for r in body["recipients"]] == [[TEST_PHONE], [TEST_PHONE_2]]
    assert "recipient_result_schema" in body
    assert [r.recipient_ref for r in run.recipient_results] == ["ent-a", "ent-b"]
    assert [r.structured_result for r in run.recipient_results] == [
        {"can_attend": "yes"},
        {"can_attend": "no"},
    ]
    assert all(r.status is RecipientStatus.COMPLETED for r in run.recipient_results)
    assert all(TEST_PHONE not in r.phone_masked for r in run.recipient_results)
    assert TEST_PHONE not in run.recipient_masked and TEST_PHONE_2 not in run.recipient_masked
    assert run.transcript_reference is not None and "attempt:att_" in run.transcript_reference
    assert TEST_PHONE not in run.summary and all(TEST_PHONE not in e for e in run.evidence)


# --- events -------------------------------------------------------------------------------


async def test_events_follow_next_cursor(env: Env) -> None:
    mission = await env.mission()
    _, authorized = await env.approved_intent(mission)
    run = await env.provider.execute(authorized)
    assert run.calle_call_id is not None
    page = await env.provider.get_events(run.calle_call_id, limit=2)
    assert len(page.events) == 2 and page.next_cursor == "2"
    everything = await env.provider.get_all_events(run.calle_call_id, limit=1)
    assert [e.event_type for e in everything] == [
        "call.queued",
        "call.in_progress",
        "call.completed",
    ]
    cursors = [
        r.url.params.get("cursor")
        for r in env.mock.requests
        if r.url.path.endswith("/events") and r.url.params.get("limit") == "1"
    ]
    assert cursors == [None, "1", "2"]
    assert all(TEST_PHONE not in e.data["message"] for e in everything)
    result = await env.provider.get_result(run.calle_call_id)
    assert result.is_simulated is False and result.structured_result == {"answer_status": "yes"}


# --- errors -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "kind"),
    [
        (401, "unauthorized", "auth"),
        (403, "forbidden", "auth"),
        (429, "rate_limit_exceeded", "rate_limited"),
        (422, "result_schema_invalid", "result_schema_invalid"),
        (400, "invalid_phone", "invalid_phone"),
    ],
)
async def test_4xx_errors_are_typed_from_the_envelope(
    env: Env, status: int, code: str, kind: str
) -> None:
    mission = await env.mission()
    _, authorized = await env.approved_intent(mission)
    headers = {"Retry-After": "7"} if status == 429 else {}
    env.mock.create_faults.append(MockCalle.error(status, code, "envelope message", **headers))
    with pytest.raises(CalleAPIError) as info:
        await env.provider.execute(authorized)
    assert info.value.kind == kind
    assert info.value.code == code
    assert info.value.status_code == status
    assert "envelope message" in str(info.value)
    assert info.value.retry_after == (7.0 if status == 429 else None)
    assert len(env.mock.creates) == 1, "no retry on a definitive 4xx"


@pytest.mark.parametrize(
    "fault",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("down"),
        MockCalle.error(500, "internal_error", "boom"),
        MockCalle.error(503, "provider_unavailable", "busy"),
    ],
    ids=["timeout", "transport", "500", "503"],
)
async def test_5xx_timeout_and_transport_are_ambiguous(env: Env, fault: Any) -> None:
    mission = await env.mission()
    _, authorized = await env.approved_intent(mission)
    env.mock.create_faults.append(fault)
    with pytest.raises(AmbiguousCreate) as info:
        await env.provider.execute(authorized)
    assert info.value.idempotency_key == authorized.plan.idempotency_key
    assert len(env.mock.creates) == 1, "the provider itself never retries a create"


# --- reconcile -------------------------------------------------------------------------


async def test_reconcile_replays_identical_bytes_under_the_same_key(env: Env) -> None:
    mission = await env.mission()
    intent, authorized = await env.approved_intent(mission)
    env.mock.create_faults.append(httpx.ReadTimeout("slow"))
    with pytest.raises(AmbiguousCreate):
        await env.provider.execute(authorized)
    run = await env.provider.reconcile(intent)
    assert run.calle_call_id == "call_001" and run.status is CallStatus.COMPLETED
    first, second = env.mock.creates
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert first.content == second.content
    # A second reconcile finds the original task under the same key: no new dial.
    again = await env.provider.reconcile(intent)
    assert again.calle_call_id == "call_001" and len(env.mock.tasks) == 1


async def test_reconcile_refuses_when_the_request_would_differ(env: Env) -> None:
    mission = await env.mission()
    intent, authorized = await env.approved_intent(mission)
    env.mock.create_faults.append(httpx.ConnectError("down"))
    with pytest.raises(AmbiguousCreate):
        await env.provider.execute(authorized)
    changed = intent.model_copy(update={"call_goal": "A different goal entirely."})
    with pytest.raises(CallProviderError, match="Refusing to dial"):
        await env.provider.reconcile(changed)
    assert len(env.mock.creates) == 1


async def test_reconcile_never_starts_a_call(env: Env) -> None:
    mission = await env.mission()
    intent = await persist_intent(env.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    with pytest.raises(CallProviderError, match="never starts one"):
        await env.provider.reconcile(intent)
    assert env.mock.creates == []


async def test_service_reconciles_a_transient_ambiguous_create_and_continues(env: Env) -> None:
    mission = await env.mission()
    intent = await persist_intent(env.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    await approved(env.database, intent)
    env.mock.create_faults.append(MockCalle.error(502, "internal_error", "gateway"))
    result = await env.service.execute_call(intent.id)
    assert result.run.status is CallStatus.COMPLETED and result.run.is_simulated is False
    keys = [r.headers["Idempotency-Key"] for r in env.mock.creates]
    assert keys == [idempotency_key(intent)] * 2, "same key twice, no other create"
    assert len(env.mock.tasks) == 1
    async with env.database.session() as session:
        stored = await MissionRepository(session).get(mission.id)
    assert stored is not None and stored.status is MissionStatus.CALL_RESULT_RECEIVED
    summaries = [e["summary"] for e in await env.events(mission.id)]
    assert any("reconciling by identical replay" in s for s in summaries)
    assert any(s.startswith("Reconciled") for s in summaries)
    assert result.claims and all(c.source_type is SourceType.PHONE for c in result.claims)
    structured = [c for c in result.claims if c.evidence_status is EvidenceStatus.PHONE_SUPPORTED]
    assert {c.predicate for c in structured} == {"answer_status"}


async def test_service_blocks_the_mission_when_reconcile_fails(env: Env) -> None:
    mission = await env.mission()
    intent = await persist_intent(env.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    await approved(env.database, intent)
    env.mock.create_faults.extend([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow again")])
    with pytest.raises(AmbiguousCreate):
        await env.service.execute_call(intent.id)
    async with env.database.session() as session:
        stored = await MissionRepository(session).get(mission.id)
        runs = await CallRunRepository(session).list_by_mission(mission.id)
    assert stored is not None and stored.status is MissionStatus.BLOCKED
    assert stored.blocker is not None and "ambiguous create" in stored.blocker
    assert runs == []
    assert len(env.mock.creates) == 2, "replay under the same key, then stop"


async def test_service_blocks_on_an_unrecoverable_error(env: Env) -> None:
    mission = await env.mission()
    intent = await persist_intent(env.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    await approved(env.database, intent)
    env.mock.create_faults.append(MockCalle.error(403, "forbidden", "no"))
    with pytest.raises(CalleAPIError, match="auth"):
        await env.service.execute_call(intent.id)
    async with env.database.session() as session:
        stored = await MissionRepository(session).get(mission.id)
    assert stored is not None and stored.status is MissionStatus.BLOCKED


# --- null result through the service -------------------------------------------------


async def test_null_structured_result_is_an_explicit_outcome(env: Env) -> None:
    env.mock.structured_result = None
    mission = await env.mission()
    intent = await persist_intent(env.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    await approved(env.database, intent)
    result = await env.service.execute_call(intent.id)
    assert result.run.status is CallStatus.COMPLETED
    assert result.run.structured_result is None
    assert result.claims, "summary and evidence survive as low-confidence claims"
    assert all(c.evidence_status is EvidenceStatus.UNKNOWN for c in result.claims)
    assert all(c.source_type is SourceType.PHONE for c in result.claims)
    assert all(TEST_PHONE not in str(c.value) for c in result.claims)
    summaries = [e["summary"] for e in await env.events(mission.id)]
    assert any("gaps stay UNKNOWN" in s for s in summaries)


async def test_schema_invalid_result_is_not_turned_into_claims(env: Env) -> None:
    env.mock.structured_result = {"answer_status": "maybe"}
    mission = await env.mission()
    intent = await persist_intent(env.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    await approved(env.database, intent)
    result = await env.service.execute_call(intent.id)
    assert result.result_validation_failed is True
    assert not [c for c in result.claims if c.evidence_status is EvidenceStatus.PHONE_SUPPORTED]


# --- polling ------------------------------------------------------------------------------


async def test_wait_for_terminal_backs_off_and_returns_terminal(env: Env) -> None:
    env.mock.initial_status = "queued"
    env.mock.status_sequence.extend(["queued", "in_progress", "in_progress", "completed"])
    mission = await env.mission()
    _, authorized = await env.approved_intent(mission)
    run = await env.provider.execute(authorized)
    assert run.status is CallStatus.COMPLETED
    gets = [r for r in env.mock.requests if r.method == "GET"]
    assert len(gets) == 4
    assert env.sleeps == [0.01, 0.02, 0.04]


async def test_wait_for_terminal_times_out(env: Env) -> None:
    env.mock.initial_status = "queued"
    env.mock.status_sequence.extend(["queued"] * 50)
    mission = await env.mission()
    _, authorized = await env.approved_intent(mission)
    with pytest.raises(CallPollTimeout, match="still queued"):
        await env.provider.execute(authorized)
    assert sum(env.sleeps) <= 1.0 + 1e-9
    assert max(env.sleeps) <= 0.08, "backoff is capped at eight times the base interval"


# --- the gate lives in the provider -------------------------------------------------


async def test_pending_approval_raises_before_any_http(env: Env) -> None:
    mission = await env.mission()
    intent = await persist_intent(env.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    plan = await env.provider.plan_call(intent)
    for status in (ApprovalStatus.PENDING, ApprovalStatus.REJECTED):
        approval = await approval_in(env.database, intent, status)
        with pytest.raises(CallNotAuthorized, match=status.value):
            await env.provider.execute(AuthorizedPlan(plan=plan, approval_id=approval.id))
    expired = await approval_in(env.database, intent, ApprovalStatus.APPROVED, expired=True)
    with pytest.raises(CallNotAuthorized, match="EXPIRED"):
        await env.provider.execute(AuthorizedPlan(plan=plan, approval_id=expired.id))
    with pytest.raises(CallNotAuthorized, match="no approval"):
        await env.provider.execute(AuthorizedPlan(plan=plan, approval_id="forged"))
    assert env.mock.requests == []


async def test_live_switch_off_refuses_before_http(tmp_path: Path) -> None:
    settings = calle_settings(tmp_path, calle_live_calls_enabled=False)
    database = Database(settings.database_url.get_secret_value())
    await database.create_schema()
    e = Env(settings, database, MockCalle())
    try:
        mission = await e.mission()
        _, authorized = await e.approved_intent(mission)
        with pytest.raises(LiveCallsDisabled):
            await e.provider.execute(authorized)
        assert e.mock.requests == []
        intent = await persist_intent(database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
        await approved(database, intent)
        with pytest.raises(LiveCallsDisabled):
            await e.service.execute_call(intent.id)
        assert e.mock.requests == []
    finally:
        await e.provider.aclose()
        await database.dispose()


async def test_empty_allow_list_refuses_before_http(tmp_path: Path) -> None:
    settings = calle_settings(tmp_path, call_allowed_recipients=[])
    database = Database(settings.database_url.get_secret_value())
    await database.create_schema()
    e = Env(settings, database, MockCalle())
    try:
        mission = await e.mission()
        _, authorized = await e.approved_intent(mission)
        with pytest.raises(RecipientNotAllowed, match="allow_list"):
            await e.provider.execute(authorized)
        assert e.mock.requests == []
    finally:
        await e.provider.aclose()
        await database.dispose()


# --- masking ----------------------------------------------------------------------------


async def test_numbers_are_masked_in_every_log_record_and_event(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="callswarm")
    mission = await env.mission()
    intent = await persist_intent(env.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    await approved(env.database, intent)
    env.mock.create_faults.append(httpx.ReadTimeout("slow"))
    logging.getLogger("callswarm.calls.calle").warning(
        "deliberate probe mentioning %s in a log line", TEST_PHONE
    )
    await env.service.execute_call(intent.id)
    # Only CallSwarm's own loggers are in scope: the database driver's DEBUG
    # output echoes stored rows, and the full number lives in the database by design.
    records = [r for r in caplog.records if r.name.startswith("callswarm")]
    assert any(r.name == "callswarm.calls.calle" for r in records), "the provider logs"
    for record in records:
        assert TEST_PHONE not in record.getMessage(), record.getMessage()
        assert TEST_PHONE not in str(record.args)
    assert any("probe" in r.getMessage() and "••" in r.getMessage() for r in records)
    for event in await env.events(mission.id):
        assert TEST_PHONE not in json.dumps(event, default=str)
    body_text = env.mock.creates[0].content.decode()
    assert not any(body_text[:40] in r.getMessage() for r in records), "body never logged"


async def test_cancel_local_refuses_an_executed_intent(env: Env) -> None:
    mission = await env.mission()
    intent = await persist_intent(env.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    await approved(env.database, intent)
    await env.service.execute_call(intent.id)
    from callswarm.calls.provider import IntentAlreadyExecuted

    with pytest.raises(IntentAlreadyExecuted):
        await env.provider.cancel_local(intent)
    fresh = await persist_intent(env.database, make_intent(mission.id))
    canceled = await env.provider.cancel_local(fresh)
    assert canceled.rejection_reason == "canceled locally before execution"


async def test_webhook_mode_leaves_the_mission_running_until_finalized(webhook_env: Env) -> None:
    e = webhook_env
    mission = await e.mission()
    intent = await persist_intent(e.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    await approved(e.database, intent)
    result = await e.service.execute_call(intent.id)
    assert result.pending is True and result.run.status is CallStatus.QUEUED
    async with e.database.session() as session:
        stored = await MissionRepository(session).get(mission.id)
        runs = await CallRunRepository(session).list_by_mission(mission.id)
        claims = await EvidenceClaimRepository(session).list_by_mission(mission.id)
    assert stored is not None and stored.status is MissionStatus.CALL_EXECUTION_RUNNING
    assert len(runs) == 1 and runs[0].status is CallStatus.QUEUED
    assert stored.call_budget.calls_used == 1, "a dial happened; it counts now"
    assert claims == []
    # A repeat execute is a no-op returning the pending run.
    again = await e.service.execute_call(intent.id)
    assert again.pending is True and again.run.id == result.run.id
    assert len(e.mock.creates) == 1

    # The terminal state arrives later; finalize re-reads it.
    task = e.mock.tasks["call_001"]
    task.update(
        status="completed",
        structured_result={"answer_status": "no"},
        summary="Declined.",
        task_completed=True,
        completion_confidence={"score": 0.8, "label": "high"},
        evidence=["They said no."],
        completed_at="2026-06-01T17:01:00Z",
    )
    final = await e.service.finalize_run("call_001")
    assert final.pending is False and final.run.status is CallStatus.COMPLETED
    assert final.run.id == result.run.id
    assert {c.predicate: c.value for c in final.claims if c.predicate == "answer_status"} == {
        "answer_status": "no"
    }
    async with e.database.session() as session:
        stored = await MissionRepository(session).get(mission.id)
    assert stored is not None and stored.status is MissionStatus.CALL_RESULT_RECEIVED
    # Finalizing again is a no-op: no duplicate claims.
    repeat = await e.service.finalize_run("call_001")
    assert [c.id for c in repeat.claims] == [c.id for c in final.claims]


async def test_concurrent_finalize_writes_one_claim_set_and_one_event_set(
    webhook_env: Env,
) -> None:
    """Project-level and per-request webhooks deliver the same terminal state
    under different event ids, so two finalizers race on one QUEUED run. The
    conditional UPDATE lets exactly one promote it; the other returns the
    winner's claims. No claim or event is written twice."""
    e = webhook_env
    mission = await e.mission()
    intent = await persist_intent(e.database, make_intent(mission.id, result_schema=GOOD_SCHEMA))
    await approved(e.database, intent)
    pending = await e.service.execute_call(intent.id)
    assert pending.pending is True
    events_before = len(await e.events(mission.id))
    e.mock.tasks["call_001"].update(
        status="completed",
        structured_result={"answer_status": "yes"},
        summary="Confirmed.",
        task_completed=True,
        completion_confidence={"score": 0.9, "label": "high"},
        evidence=["They confirmed."],
        completed_at="2026-06-01T17:01:00Z",
    )

    results = await asyncio.gather(
        *(e.service.finalize_run("call_001") for _ in range(4)), return_exceptions=True
    )
    for outcome in results:
        assert not isinstance(outcome, BaseException), outcome
        assert outcome.pending is False and outcome.run.status is CallStatus.COMPLETED

    async with e.database.session() as session:
        claims = await EvidenceClaimRepository(session).list_by_mission(mission.id)
        stored = await MissionRepository(session).get(mission.id)
    by_predicate = [c.predicate for c in claims]
    assert by_predicate.count("answer_status") == 1, by_predicate
    assert len(by_predicate) == len(set(by_predicate)), "every claim written exactly once"
    final_ids = {c.id for c in claims}
    returned = [{c.id for c in r.claims} for r in results if not isinstance(r, BaseException)]
    assert final_ids in returned, "the winner returns the claims it wrote"
    assert all(ids <= final_ids for ids in returned), "losers only ever see stored claims"
    events = (await e.events(mission.id))[events_before:]
    recorded = [ev for ev in events if ev["summary"].startswith("Recorded ")]
    assert len(recorded) == 1, [ev["summary"] for ev in events]
    assert len([ev for ev in events if ev.get("status") == "completed"]) == 1
    assert stored is not None and stored.status is MissionStatus.CALL_RESULT_RECEIVED
    assert stored.call_budget.calls_used == 1


def test_event_types_used_by_the_service_are_real() -> None:
    assert ActivityEventType.CALL_EVENT.value == "CALL_EVENT"
    assert datetime.now(UTC) <= utcnow()
