"""CS-036: the CALL-E terminal webhook receiver.

No sockets. The app is built with ``CALL_PROVIDER=calle`` and its provider is
swapped for a :class:`CalleProvider` whose HTTP goes through the recorded
``MockCalle`` transport from ``test_calls_calle``. The webhook is posted with
``httpx.ASGITransport``. Every test runs with live calls enabled only because
the run under test must be *non-simulated* (the receiver rejects simulated
runs); nothing here reaches a network.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from callswarm.api.app import create_app
from callswarm.api.webhooks import (
    EVENT_ID_HEADER,
    OUTCOME_FAILED,
    OUTCOME_NOT_TERMINAL,
    OUTCOME_PROCESSED,
    OUTCOME_REJECTED_PAYLOAD,
    OUTCOME_REJECTED_UNCORRELATED,
)
from callswarm.approvals import ApprovalService
from callswarm.calls.calle import CalleProvider
from callswarm.calls.service import LOW_CONFIDENCE_LABEL, CallService
from callswarm.models import (
    CallIntent,
    CallRun,
    CallStatus,
    EvidenceClaim,
    EvidenceStatus,
    Mission,
    MissionStatus,
    SourceType,
    WebhookReceipt,
)
from callswarm.persistence import (
    CallRunRepository,
    Database,
    EvidenceClaimRepository,
    MissionRepository,
    WebhookReceiptRepository,
)
from callswarm.persistence.orm import WebhookReceiptRow
from tests.conftest import TEST_PHONE, make_intent, persist_intent
from tests.test_calls_calle import MockCalle, calle_settings
from tests.test_calls_provider import GOOD_SCHEMA, approved

SECRET = "webhook-secret-for-tests-only"
FORGED_ANSWER = "forged-answer-never-stored"
FORGED_SUMMARY = "FORGED SUMMARY MUST NOT BE STORED"


class WebhookEnv:
    """A started app whose CALL-E provider talks to ``MockCalle``."""

    def __init__(self, app: FastAPI, mock: MockCalle) -> None:
        self.app = app
        self.mock = mock
        self.database: Database = app.state.database
        self.settings = app.state.settings

    @property
    def provider(self) -> CalleProvider:
        provider: CalleProvider = self.app.state.call_provider
        return provider

    def client(self, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=raise_app_exceptions),
            base_url="http://testserver",
        )

    def service(self) -> CallService:
        state = self.app.state
        approvals = ApprovalService(
            state.database, state.emitter, state.settings, state.state_machine
        )
        return CallService(
            state.call_provider,
            state.call_gate,
            approvals,
            state.database,
            state.emitter,
            state.settings,
            state.state_machine,
        )

    async def queued_run(self) -> tuple[Mission, CallIntent, CallRun]:
        """Dial through the real service in webhook mode: the run is QUEUED
        and the mission sits in CALL_EXECUTION_RUNNING."""
        async with self.database.session() as session:
            mission = await MissionRepository(session).add(
                Mission(
                    user_goal="test goal",
                    status=MissionStatus.CALL_AUTHORIZED,
                    authority_policy={"calls_allowed": True, "max_call_count": 3},
                    call_budget={"max_calls": 3},
                )
            )
        intent = await persist_intent(
            self.database, make_intent(mission.id, result_schema=GOOD_SCHEMA)
        )
        await approved(self.database, intent)
        result = await self.service().execute_call(intent.id)
        assert result.pending is True and result.run.status is CallStatus.QUEUED
        assert result.run.is_simulated is False
        return mission, intent, result.run

    def complete_remote(self, call_id: str, **overrides: Any) -> None:
        """Make the mock's authoritative state terminal."""
        task = self.mock.tasks[call_id]
        task.update(
            status="completed",
            structured_result={"answer_status": "yes"},
            summary="Authoritative summary.",
            task_completed=True,
            completion_confidence={"score": 0.9, "label": "high"},
            evidence=["Authoritative evidence line."],
            completed_at="2026-06-01T17:01:00Z",
        )
        task.update(overrides)

    async def mission_status(self, mission_id: str) -> MissionStatus:
        async with self.database.session() as session:
            mission = await MissionRepository(session).get(mission_id)
        assert mission is not None
        return mission.status

    async def claims(self, mission_id: str) -> list[EvidenceClaim]:
        async with self.database.session() as session:
            return await EvidenceClaimRepository(session).list_by_mission(mission_id)

    async def receipts(self) -> list[WebhookReceipt]:
        async with self.database.session() as session:
            result = await session.execute(select(WebhookReceiptRow))
            ids = [row.id for row in result.scalars()]
            repo = WebhookReceiptRepository(session)
            receipts = [await repo.get(id_) for id_ in ids]
        return [r for r in receipts if r is not None]

    async def run(self, call_id: str) -> CallRun:
        async with self.database.session() as session:
            stored = await CallRunRepository(session).get_by_calle_call_id(call_id)
        assert stored is not None
        return stored


async def start_env(tmp_path: Path, **overrides: Any) -> tuple[WebhookEnv, Any]:
    base: dict[str, Any] = {
        "calle_webhook_url": f"https://example.test/calle/webhook/{SECRET}",
        "calle_webhook_secret": SECRET,
    }
    base.update(overrides)
    settings = calle_settings(tmp_path, **base)
    app = create_app(settings)
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    mock = MockCalle()
    mock.initial_status = "queued"
    original = app.state.call_provider
    await original.aclose()
    app.state.call_provider = CalleProvider(
        app.state.call_gate, settings, app.state.database, transport=mock.transport
    )
    return WebhookEnv(app, mock), context


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[WebhookEnv]:
    e, context = await start_env(tmp_path)
    try:
        yield e
    finally:
        await context.__aexit__(None, None, None)


def payload(
    call_id: str,
    mission_id: str,
    intent_id: str,
    *,
    event_type: str = "call.completed",
    status: str = "completed",
    **extra: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": call_id,
        "object": "call_task",
        "status": status,
        "task": "irrelevant",
        "recipients": [],
        "structured_result": None,
        "metadata": {"mission_id": mission_id, "call_intent_id": intent_id},
        "created_at": "2026-06-01T17:00:00Z",
    }
    data.update(extra)
    return {
        "id": "evt_test_1",
        "object": "event",
        "type": event_type,
        "created_at": "2026-06-01T17:01:00Z",
        "data": data,
    }


async def post(
    e: WebhookEnv,
    body: Any,
    *,
    token: str = SECRET,
    event_id: str | None = "evt-1",
    raise_app_exceptions: bool = True,
) -> httpx.Response:
    headers = {} if event_id is None else {EVENT_ID_HEADER: event_id}
    async with e.client(raise_app_exceptions=raise_app_exceptions) as http:
        return await http.post(f"/calle/webhook/{token}", json=body, headers=headers)


# --- access control ----------------------------------------------------------------------


async def test_wrong_token_is_404_and_leaves_no_receipt(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    response = await post(
        env, payload(run.calle_call_id or "", mission.id, intent.id), token="nope"
    )
    assert response.status_code == 404
    assert await env.receipts() == []
    assert await env.mission_status(mission.id) is MissionStatus.CALL_EXECUTION_RUNNING


async def test_no_secret_configured_means_receiver_is_off(tmp_path: Path) -> None:
    # Settings refuse a webhook URL without a secret at startup (see
    # test_config); the receiver must still be closed if the secret is absent
    # at request time, so the running app's settings are swapped for a copy
    # without one (``model_copy`` bypasses validation on purpose).
    e, context = await start_env(tmp_path)
    try:
        mission, intent, run = await e.queued_run()
        e.app.state.settings = e.settings.model_copy(update={"calle_webhook_secret": None})
        e.complete_remote(run.calle_call_id or "")
        for token in ("", "None", "anything"):
            response = await post(
                e, payload(run.calle_call_id or "", mission.id, intent.id), token=token
            )
            assert response.status_code == 404
        assert await e.receipts() == []
        assert await e.claims(mission.id) == []
    finally:
        await context.__aexit__(None, None, None)


async def test_missing_event_id_is_400(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    body = payload(run.calle_call_id or "", mission.id, intent.id)
    response = await post(env, body, event_id=None)
    assert response.status_code == 400
    blank = await post(env, body, event_id="   ")
    assert blank.status_code == 400
    assert await env.receipts() == []


# --- idempotency --------------------------------------------------------------------------


async def test_duplicate_event_id_is_a_no_op_with_one_receipt(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    env.complete_remote(call_id)
    body = payload(call_id, mission.id, intent.id)

    first = await post(env, body, event_id="evt-dup")
    assert first.status_code == 200 and first.json()["outcome"] == OUTCOME_PROCESSED
    claims_after_first = await env.claims(mission.id)
    assert claims_after_first, "the happy path wrote claims"
    status_calls = len([r for r in env.mock.requests if r.method == "GET"])

    second = await post(env, body, event_id="evt-dup")
    assert second.status_code == 200
    assert second.json() == {"ok": True, "duplicate": True, "outcome": "duplicate"}
    receipts = await env.receipts()
    assert [r.event_id for r in receipts] == ["evt-dup"]
    assert receipts[0].outcome == OUTCOME_PROCESSED
    # No second side effect: no re-read, no new claims.
    assert len([r for r in env.mock.requests if r.method == "GET"]) == status_calls
    assert [c.id for c in await env.claims(mission.id)] == [c.id for c in claims_after_first]


# --- correlation --------------------------------------------------------------------------


async def test_unknown_call_id_is_404(env: WebhookEnv) -> None:
    mission, intent, _ = await env.queued_run()
    response = await post(env, payload("call_does_not_exist", mission.id, intent.id))
    assert response.status_code == 404
    receipts = await env.receipts()
    assert len(receipts) == 1 and receipts[0].outcome == OUTCOME_REJECTED_UNCORRELATED
    assert await env.claims(mission.id) == []
    assert not [r for r in env.mock.requests if r.method == "GET"], "no re-read for a stranger"


@pytest.mark.parametrize(
    "metadata",
    [
        {"mission_id": "other-mission", "call_intent_id": "{intent}"},
        {"mission_id": "{mission}", "call_intent_id": "other-intent"},
        {},
        {"mission_id": "{mission}"},
    ],
    ids=["wrong_mission", "wrong_intent", "no_metadata", "no_intent"],
)
async def test_mismatched_metadata_is_404(env: WebhookEnv, metadata: dict[str, str]) -> None:
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    env.complete_remote(call_id)
    resolved = {
        k: v.replace("{mission}", mission.id).replace("{intent}", intent.id)
        for k, v in metadata.items()
    }
    body = payload(call_id, mission.id, intent.id, metadata=resolved)
    response = await post(env, body)
    assert response.status_code == 404
    assert await env.claims(mission.id) == []
    assert await env.mission_status(mission.id) is MissionStatus.CALL_EXECUTION_RUNNING
    stored = await env.run(call_id)
    assert stored.status is CallStatus.QUEUED


async def test_off_contract_payload_is_400_after_receipt(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    body = payload(run.calle_call_id or "", mission.id, intent.id, event_type="call.queued")
    response = await post(env, body)
    assert response.status_code == 400
    receipts = await env.receipts()
    assert len(receipts) == 1 and receipts[0].outcome == OUTCOME_REJECTED_PAYLOAD
    not_json = await post(env, ["not", "an", "object"], event_id="evt-list")
    assert not_json.status_code == 400
    assert await env.claims(mission.id) == []


# --- the payload is a notification only -------------------------------------------------


async def test_forged_structured_result_never_becomes_evidence(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    env.complete_remote(call_id)  # authoritative: answer_status == "yes"
    forged = payload(
        call_id,
        mission.id,
        intent.id,
        structured_result={"answer_status": FORGED_ANSWER, "injected_field": "x"},
        summary=FORGED_SUMMARY,
        evidence=["forged evidence line"],
        task_completed=True,
    )
    response = await post(env, forged)
    assert response.status_code == 200 and response.json()["outcome"] == OUTCOME_PROCESSED

    claims = await env.claims(mission.id)
    by_predicate = {c.predicate: c for c in claims}
    assert by_predicate["answer_status"].value == "yes"
    assert by_predicate["answer_status"].value != FORGED_ANSWER
    assert by_predicate["answer_status"].evidence_status is EvidenceStatus.PHONE_SUPPORTED
    assert by_predicate["answer_status"].source_type is SourceType.PHONE
    assert "injected_field" not in by_predicate
    values = " ".join(str(c.value) for c in claims)
    assert FORGED_ANSWER not in values and FORGED_SUMMARY not in values and "forged" not in values
    assert "Authoritative summary." in values

    stored = await env.run(call_id)
    assert stored.status is CallStatus.COMPLETED
    assert stored.structured_result == {"answer_status": "yes"}
    assert stored.summary == "Authoritative summary."
    # The re-read happened via GET /v1/calls/{id}.
    gets = [r for r in env.mock.requests if r.method == "GET"]
    assert gets and gets[-1].url.path == f"/v1/calls/{call_id}"


async def test_forged_call_failed_does_not_fail_a_completed_call(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    env.complete_remote(call_id)
    forged = payload(
        call_id,
        mission.id,
        intent.id,
        event_type="call.failed",
        status="failed",
        failure_code="forged",
        failure_message="forged",
    )
    response = await post(env, forged)
    assert response.status_code == 200
    stored = await env.run(call_id)
    assert stored.status is CallStatus.COMPLETED
    assert stored.failure_code is None


async def test_result_validation_failed_stores_no_structured_result(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    # Authoritative state even carries a (schema-valid) result; the event says
    # CALL-E's own validation failed, so it is recorded without one.
    env.complete_remote(call_id)
    body = payload(call_id, mission.id, intent.id, event_type="call.result_validation_failed")
    response = await post(env, body)
    assert response.status_code == 200 and response.json()["outcome"] == OUTCOME_PROCESSED
    stored = await env.run(call_id)
    assert stored.status is CallStatus.COMPLETED and stored.structured_result is None
    claims = await env.claims(mission.id)
    assert claims, "summary and evidence still become low-confidence claims"
    assert all(c.predicate != "answer_status" for c in claims)
    assert all(c.evidence_status is EvidenceStatus.UNKNOWN for c in claims)
    assert all(LOW_CONFIDENCE_LABEL in c.source_reference for c in claims)
    assert await env.mission_status(mission.id) is MissionStatus.CALL_RESULT_RECEIVED


# --- receipt-before-side-effect ---------------------------------------------------------


async def test_receipt_is_persisted_before_the_side_effect(
    env: WebhookEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    env.complete_remote(call_id)

    async def boom(_: str) -> CallRun:
        raise RuntimeError("re-read exploded")

    monkeypatch.setattr(env.provider, "get_status", boom)
    response = await post(
        env,
        payload(call_id, mission.id, intent.id),
        event_id="evt-boom",
        raise_app_exceptions=False,
    )
    assert response.status_code >= 500
    receipts = await env.receipts()
    assert [r.event_id for r in receipts] == ["evt-boom"]
    assert receipts[0].outcome == OUTCOME_FAILED
    assert await env.claims(mission.id) == []
    assert await env.mission_status(mission.id) is MissionStatus.CALL_EXECUTION_RUNNING
    assert (await env.run(call_id)).status is CallStatus.QUEUED

    # A re-delivery after a failed attempt is the one case that retries.
    monkeypatch.undo()
    retry = await post(env, payload(call_id, mission.id, intent.id), event_id="evt-boom")
    assert retry.status_code == 200 and retry.json()["outcome"] == OUTCOME_PROCESSED
    receipts = await env.receipts()
    assert len(receipts) == 1 and receipts[0].outcome == OUTCOME_PROCESSED
    assert await env.mission_status(mission.id) is MissionStatus.CALL_RESULT_RECEIVED


# --- mission transition -----------------------------------------------------------------


async def test_accept_transitions_running_mission_to_result_received(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    assert await env.mission_status(mission.id) is MissionStatus.CALL_EXECUTION_RUNNING
    env.complete_remote(call_id)
    response = await post(env, payload(call_id, mission.id, intent.id))
    assert response.status_code == 200
    assert await env.mission_status(mission.id) is MissionStatus.CALL_RESULT_RECEIVED


async def test_non_terminal_re_read_changes_nothing(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    # Remote still in progress even though the notification claims completion.
    env.mock.tasks[call_id]["status"] = "in_progress"
    response = await post(env, payload(call_id, mission.id, intent.id))
    assert response.status_code == 200 and response.json()["outcome"] == OUTCOME_NOT_TERMINAL
    assert await env.claims(mission.id) == []
    assert await env.mission_status(mission.id) is MissionStatus.CALL_EXECUTION_RUNNING


async def test_no_transition_when_mission_is_elsewhere(env: WebhookEnv) -> None:
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    async with env.database.session() as session:
        missions = MissionRepository(session)
        current = await missions.get(mission.id)
        assert current is not None
        await missions.update(current.model_copy(update={"status": MissionStatus.BLOCKED}))
    env.complete_remote(call_id)
    response = await post(env, payload(call_id, mission.id, intent.id))
    assert response.status_code == 200 and response.json()["outcome"] == OUTCOME_PROCESSED
    assert await env.mission_status(mission.id) is MissionStatus.BLOCKED
    assert (await env.run(call_id)).status is CallStatus.COMPLETED


# --- logging ------------------------------------------------------------------------------


async def test_logs_carry_no_payload_and_no_phone_number(
    env: WebhookEnv, caplog: pytest.LogCaptureFixture
) -> None:
    # INFO: the receiver, provider and service log at INFO and above. (The
    # aiosqlite driver echoes bound SQL parameters at DEBUG; that is the
    # database, not a CallSwarm log line.)
    caplog.set_level(logging.INFO)
    mission, intent, run = await env.queued_run()
    call_id = run.calle_call_id or ""
    env.complete_remote(call_id)
    forged = payload(
        call_id,
        mission.id,
        intent.id,
        structured_result={"answer_status": FORGED_ANSWER},
        summary=FORGED_SUMMARY,
        recipients=[{"phones": [TEST_PHONE]}],
    )
    ok = await post(env, forged, event_id="evt-log-1")
    assert ok.status_code == 200
    rejected = await post(
        env,
        payload("call_unknown", mission.id, intent.id, summary=FORGED_SUMMARY),
        event_id="evt-log-2",
    )
    assert rejected.status_code == 404
    bad = await post(env, {"garbage": FORGED_SUMMARY}, event_id="evt-log-3")
    assert bad.status_code == 400

    text = caplog.text
    assert TEST_PHONE not in text
    assert TEST_PHONE[-4:] not in text.replace("evt-log", "")
    assert FORGED_ANSWER not in text and FORGED_SUMMARY not in text
    assert "garbage" not in text
    assert "evt-log-1" in text and "evt-log-2" in text and "evt-log-3" in text
