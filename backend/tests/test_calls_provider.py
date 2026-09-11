"""CS-031: provider abstraction, fake provider, no network, SIMULATED end to end."""

from __future__ import annotations

import inspect
import socket
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from callswarm import calls as calls_pkg
from callswarm.api.app import create_app
from callswarm.approvals import ApprovalService
from callswarm.calls.fake import NO_SCRIPT_SUMMARY, FakeCallProvider, FakeScript
from callswarm.calls.provider import (
    AuthorizedPlan,
    CallExecutionProvider,
    CallNotAuthorized,
    CallProviderError,
    CallProviderNotAvailable,
    GatedCallProvider,
    IntentAlreadyExecuted,
    idempotency_key,
)
from callswarm.calls.service import CallService, claims_from_run, select_call_provider
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.models import (
    Approval,
    ApprovalStatus,
    ApprovalSubjectType,
    CallAuthorizationState,
    CallIntent,
    CallStatus,
    EvidenceStatus,
    Mission,
    PlanComponent,
    PlanOption,
    ScheduledJob,
    ScheduledJobStatus,
    SourceType,
    utcnow,
)
from callswarm.persistence import (
    ApprovalRepository,
    CallIntentRepository,
    Database,
    ScheduledJobRepository,
)
from tests.conftest import TEST_PHONE, make_intent, persist_intent

GOOD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer_status"],
    "properties": {
        "answer_status": {
            "type": "string",
            "enum": ["yes", "no", "unknown"],
            "description": "Whether the recipient confirmed the question.",
        },
        "quoted_amount": {"type": "number", "description": "Amount stated, if any."},
    },
}


async def approved(database: Database, intent: CallIntent) -> Approval:
    now = utcnow()
    async with database.session() as session:
        return await ApprovalRepository(session).add(
            Approval(
                mission_id=intent.mission_id,
                subject_type=ApprovalSubjectType.CALL_INTENT,
                subject_id=intent.id,
                status=ApprovalStatus.APPROVED,
                requested_at=now,
                decided_at=now,
                expires_at=now + timedelta(days=30),
                decided_by="test",
            )
        )


# --- defaults and selection ---------------------------------------------------------


def test_fake_is_the_default_provider(settings: Settings) -> None:
    assert settings.call_provider == "fake"
    assert settings.calle_live_calls_enabled is False


async def test_selecting_calle_raises_and_never_falls_back(
    tmp_path: Path, database: Database, emitter: ActivityEventEmitter
) -> None:
    s = Settings(
        _env_file=None,
        call_provider="calle",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}",
    )
    with pytest.raises(CallProviderNotAvailable, match="not implemented"):
        select_call_provider(s, database, emitter)


async def test_app_startup_fails_loudly_with_calle(tmp_path: Path) -> None:
    s = Settings(
        _env_file=None,
        call_provider="calle",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}",
    )
    application = create_app(s)
    with pytest.raises(CallProviderNotAvailable):
        async with application.router.lifespan_context(application):
            pass


async def test_app_exposes_fake_provider_and_gate(app: Any) -> None:
    provider = app.state.call_provider
    assert isinstance(provider, FakeCallProvider)
    assert provider.is_simulated is True
    assert isinstance(provider, CallExecutionProvider)


def test_fake_provider_satisfies_the_protocol_surface() -> None:
    for name in (
        "plan_call",
        "authorize",
        "execute",
        "get_status",
        "get_events",
        "get_result",
        "reconcile",
        "cancel_local",
    ):
        assert callable(getattr(FakeCallProvider, name)), name
    assert not hasattr(FakeCallProvider, "cancel"), "no remote cancel exists"
    signature = inspect.signature(FakeCallProvider.get_events)
    assert list(signature.parameters)[1:] == ["calle_call_id", "cursor", "limit"]
    assert signature.parameters["limit"].default == 50


def test_fake_source_is_free_of_network_primitives() -> None:
    source = Path(inspect.getfile(FakeCallProvider)).read_text()
    for token in ("httpx", "socket", "urllib", "requests", "aiohttp"):
        assert token not in source


# --- execution through the gate -------------------------------------------------------


async def test_fake_execute_makes_no_sockets_and_is_simulated(
    fake_provider: FakeCallProvider,
    database: Database,
    call_mission: Mission,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Any] = []
    real_init = socket.socket.__init__

    def spy(self: socket.socket, *args: Any, **kwargs: Any) -> None:
        created.append(args)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "__init__", spy)
    intent = await persist_intent(database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA))
    approval = await approved(database, intent)
    fake_provider.script(
        FakeScript(structured_result={"answer_status": "yes"}, summary="They said yes.")
    )
    plan = await fake_provider.plan_call(intent)
    authorized = await fake_provider.authorize(plan, approval)
    run = await fake_provider.execute(authorized)
    assert created == []
    assert run.is_simulated is True
    assert run.status is CallStatus.COMPLETED
    assert run.structured_result == {"answer_status": "yes"}
    assert run.summary.startswith("[SIMULATED]")
    assert TEST_PHONE not in run.recipient_masked
    assert run.calle_call_id is not None
    # Real CALL-E status sequence is recorded as events.
    page = await fake_provider.get_events(run.calle_call_id)
    assert [e.data["status"] for e in page.events] == ["queued", "in_progress", "completed"]
    assert page.next_cursor is None
    first = await fake_provider.get_events(run.calle_call_id, limit=2)
    assert len(first.events) == 2 and first.next_cursor == "2"
    rest = await fake_provider.get_events(run.calle_call_id, cursor=first.next_cursor, limit=2)
    assert [e.data["status"] for e in rest.events] == ["completed"]
    assert (await fake_provider.get_status(run.calle_call_id)).status is CallStatus.COMPLETED
    result = await fake_provider.get_result(run.calle_call_id)
    assert result.is_simulated is True and result.structured_result == {"answer_status": "yes"}


async def test_every_fake_claim_is_simulated_and_propagates_to_plan_option(
    fake_provider: FakeCallProvider, database: Database, call_mission: Mission
) -> None:
    intent = await persist_intent(database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA))
    approval = await approved(database, intent)
    fake_provider.script(
        FakeScript(
            structured_result={"answer_status": "no", "quoted_amount": 12.5},
            summary="Declined.",
            evidence=["They said no."],
        )
    )
    run = await fake_provider.execute(
        await fake_provider.authorize(await fake_provider.plan_call(intent), approval)
    )
    claims = claims_from_run(run, intent, "subject", result_valid=True)
    assert claims and all(c.source_type is SourceType.SIMULATED for c in claims)
    structured = [c for c in claims if c.evidence_status is EvidenceStatus.PHONE_SUPPORTED]
    assert {c.predicate for c in structured} == {"answer_status", "quoted_amount"}
    low = [c for c in claims if c.evidence_status is EvidenceStatus.UNKNOWN]
    assert all("low-confidence" in c.source_reference for c in low)
    option = PlanOption(
        mission_id=call_mission.id,
        name="derived",
        components=[
            PlanComponent(
                name="c", source_types=list({c.source_type for c in claims}), claim_ids=[]
            )
        ],
    )
    assert option.contains_simulated_or_fixture is True
    assert SourceType.SIMULATED in option.source_types


async def test_scripted_none_failed_and_validation_failed_results(
    fake_provider: FakeCallProvider, database: Database, call_mission: Mission
) -> None:
    intents = [
        await persist_intent(database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA))
        for _ in range(3)
    ]
    fake_provider.script(FakeScript(structured_result=None, summary="Nobody answered."))
    fake_provider.script(FakeScript(outcome="failed", failure_code="no_answer"))
    fake_provider.script(
        FakeScript(outcome="result_validation_failed", structured_result={"answer_status": "yes"})
    )
    runs = []
    for intent in intents:
        approval = await approved(database, intent)
        runs.append(
            await fake_provider.execute(
                await fake_provider.authorize(await fake_provider.plan_call(intent), approval)
            )
        )
    assert runs[0].status is CallStatus.COMPLETED and runs[0].structured_result is None
    assert runs[0].task_completed is False
    assert runs[1].status is CallStatus.FAILED and runs[1].failure_code == "no_answer"
    assert runs[2].status is CallStatus.COMPLETED and runs[2].structured_result is None
    for run in runs:
        assert run.is_simulated is True
        assert all(
            c.source_type is SourceType.SIMULATED
            for c in claims_from_run(run, intents[0], "s", result_valid=True)
        )
    none_claims = claims_from_run(runs[0], intents[0], "s", result_valid=True)
    assert all(c.evidence_status is EvidenceStatus.UNKNOWN for c in none_claims)


async def test_unscripted_result_is_explicitly_empty(
    fake_provider: FakeCallProvider, database: Database, call_mission: Mission
) -> None:
    intent = await persist_intent(database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA))
    approval = await approved(database, intent)
    run = await fake_provider.execute(
        await fake_provider.authorize(await fake_provider.plan_call(intent), approval)
    )
    assert run.structured_result is None and run.summary == NO_SCRIPT_SUMMARY


async def test_schema_invalid_result_becomes_none_like_the_real_provider(
    fake_provider: FakeCallProvider, database: Database, call_mission: Mission
) -> None:
    intent = await persist_intent(database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA))
    approval = await approved(database, intent)
    fake_provider.script(FakeScript(structured_result={"answer_status": "maybe"}))
    run = await fake_provider.execute(
        await fake_provider.authorize(await fake_provider.plan_call(intent), approval)
    )
    assert run.structured_result is None


async def test_per_intent_scripts_take_precedence_over_fifo(
    fake_provider: FakeCallProvider, database: Database, call_mission: Mission
) -> None:
    a = await persist_intent(database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA))
    b = await persist_intent(database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA))
    fake_provider.script(FakeScript(structured_result={"answer_status": "no"}))  # fifo
    fake_provider.script(FakeScript(structured_result={"answer_status": "yes"}), intent_id=b.id)
    for intent, expected in ((b, "yes"), (a, "no")):
        approval = await approved(database, intent)
        run = await fake_provider.execute(
            await fake_provider.authorize(await fake_provider.plan_call(intent), approval)
        )
        assert run.structured_result == {"answer_status": expected}


# --- idempotency, reconcile, cancel_local ----------------------------------------------


def test_idempotency_key_is_deterministic_and_bounded() -> None:
    intent = make_intent("m")
    assert idempotency_key(intent) == idempotency_key(intent)
    assert len(idempotency_key(intent)) <= 255
    changed = intent.model_copy(update={"call_goal": "different"})
    assert idempotency_key(changed) != idempotency_key(intent)


async def test_reconcile_replays_under_the_same_key(
    fake_provider: FakeCallProvider, database: Database, call_mission: Mission
) -> None:
    intent = await persist_intent(database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA))
    with pytest.raises(CallProviderError, match="never starts one"):
        await fake_provider.reconcile(intent)
    approval = await approved(database, intent)
    authorized = await fake_provider.authorize(await fake_provider.plan_call(intent), approval)
    first = await fake_provider.execute(authorized)
    again = await fake_provider.execute(authorized)
    assert again.id == first.id, "same key, same request: nothing dials twice"
    replay = await fake_provider.reconcile(intent)
    assert replay.calle_call_id == first.calle_call_id


async def test_cancel_local_refuses_an_executed_intent_and_cancels_unfired_job(
    fake_provider: FakeCallProvider, database: Database, call_mission: Mission
) -> None:
    pending = await persist_intent(database, make_intent(call_mission.id))
    async with database.session() as session:
        job = await ScheduledJobRepository(session).add(
            ScheduledJob(
                mission_id=call_mission.id,
                job_type="call",
                due_at=utcnow() + timedelta(hours=2),
                payload={"call_intent_id": pending.id},
            )
        )
    canceled = await fake_provider.cancel_local(pending)
    assert canceled.authorization_state is CallAuthorizationState.BLOCKED
    async with database.session() as session:
        assert (
            await ScheduledJobRepository(session).get(job.id)
        ).status is ScheduledJobStatus.CANCELED  # type: ignore[union-attr]
        stored = await CallIntentRepository(session).get(pending.id)
    assert stored is not None and stored.rejection_reason == "canceled locally before execution"

    executed = await persist_intent(
        database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA)
    )
    approval = await approved(database, executed)
    await fake_provider.execute(
        await fake_provider.authorize(await fake_provider.plan_call(executed), approval)
    )
    with pytest.raises(IntentAlreadyExecuted, match="no cancel"):
        await fake_provider.cancel_local(executed)


# --- the gate lives in the provider --------------------------------------------------


async def test_execute_without_approval_raises_even_when_called_directly(
    fake_provider: FakeCallProvider, database: Database, call_mission: Mission
) -> None:
    intent = await persist_intent(database, make_intent(call_mission.id, result_schema=GOOD_SCHEMA))
    plan = await fake_provider.plan_call(intent)
    forged = AuthorizedPlan(plan=plan, approval_id="does-not-exist")
    with pytest.raises(CallNotAuthorized, match="no approval record"):
        await fake_provider.execute(forged)
    assert fake_provider.executed_intent_ids == []


async def test_execute_is_final_on_the_base_class() -> None:
    assert FakeCallProvider.execute is GatedCallProvider.execute
    assert "_gate.check" in inspect.getsource(GatedCallProvider.execute)


async def test_authorize_rejects_mismatched_or_unapproved(
    fake_provider: FakeCallProvider, database: Database, call_mission: Mission
) -> None:
    intent = await persist_intent(database, make_intent(call_mission.id))
    other = await persist_intent(database, make_intent(call_mission.id))
    plan = await fake_provider.plan_call(intent)
    for status in (ApprovalStatus.PENDING, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED):
        approval = Approval(
            mission_id=call_mission.id,
            subject_type=ApprovalSubjectType.CALL_INTENT,
            subject_id=intent.id,
            status=status,
        )
        with pytest.raises(CallNotAuthorized, match=status.value):
            await fake_provider.authorize(plan, approval)
    wrong = await approved(database, other)
    with pytest.raises(CallNotAuthorized, match="different call intent"):
        await fake_provider.authorize(plan, wrong)


# --- service wiring -------------------------------------------------------------------


async def test_call_service_uses_the_injected_provider(
    call_service: CallService, fake_provider: FakeCallProvider, approval_service: ApprovalService
) -> None:
    assert call_service.provider is fake_provider
    assert call_service.provider.is_simulated is True
    assert isinstance(approval_service, ApprovalService)


def test_calls_package_exports_provider_surface() -> None:
    for name in ("FakeCallProvider", "CallGate", "select_call_provider", "CallService"):
        assert name in calls_pkg.__all__
