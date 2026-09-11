"""Shared fixtures.

Every test runs with the fake call provider, live calls disabled, no ``.env``
file and the network blocked at the socket level. A test that reaches the
network or could place a real call is a defect.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from callswarm.api.app import create_app
from callswarm.approvals import ApprovalService
from callswarm.calls.fake import FakeCallProvider
from callswarm.calls.gates import CallGate
from callswarm.calls.service import CallService
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.llm import FakeLLMProvider
from callswarm.models import (
    AuthorityPolicy,
    CallAuthorizationState,
    CallBudget,
    CallIntent,
    CallRecipient,
    Mission,
    MissionStatus,
)
from callswarm.orchestrator.state_machine import MissionStateMachine
from callswarm.persistence import CallIntentRepository, Database, MissionRepository
from callswarm.sanitize import Sanitizer


class NetworkBlockedError(RuntimeError):
    pass


def _blocked_connect(self: socket.socket, address: Any) -> None:
    raise NetworkBlockedError(f"network access is blocked in tests (connect to {address!r})")


@pytest.fixture(scope="session", autouse=True)
def _safe_environment() -> Iterator[None]:
    """Force the default-off posture and block all outbound sockets for the whole suite."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CALL_PROVIDER", "fake")
        mp.setenv("CALLE_LIVE_CALLS_ENABLED", "false")
        mp.setenv("RESEARCH_PROVIDER", "fixture")
        for secret in ("GEMINI_API_KEY", "CALLE_API_KEY", "SEARCH_API_KEY", "CALLE_WEBHOOK_SECRET"):
            mp.delenv(secret, raising=False)
        mp.setattr(socket.socket, "connect", _blocked_connect)
        mp.setattr(socket.socket, "connect_ex", _blocked_connect)
        yield


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings that ignore any ``.env`` file and use a temporary SQLite file."""
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'callswarm-test.db'}",
    )


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings.database_url.get_secret_value())
    await db.create_schema()
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
def sanitizer(settings: Settings) -> Sanitizer:
    return Sanitizer(settings.reasoning_leak_markers)


@pytest.fixture
async def emitter(database: Database, sanitizer: Sanitizer) -> ActivityEventEmitter:
    return ActivityEventEmitter(database, sanitizer)


@pytest.fixture
async def mission(database: Database) -> Mission:
    async with database.session() as session:
        return await MissionRepository(session).add(Mission(user_goal="test goal"))


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest.fixture
def assert_env_is_safe() -> None:
    assert os.environ.get("CALL_PROVIDER") == "fake"
    assert os.environ.get("CALLE_LIVE_CALLS_ENABLED") == "false"


# --- Phase 2 fixtures -----------------------------------------------------------


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest.fixture
async def state_machine(database: Database, emitter: ActivityEventEmitter) -> MissionStateMachine:
    return MissionStateMachine(database, emitter)


@pytest.fixture
async def llm_app(settings: Settings, fake_llm: FakeLLMProvider) -> AsyncIterator[FastAPI]:
    """The app with the scripted fake provider injected in place of Gemini."""
    application = create_app(settings, llm_provider=fake_llm)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def llm_client(llm_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=llm_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def set_mission_status(
    database: Database, mission: Mission, status: MissionStatus
) -> Mission:
    """Test-only: force a persisted status to start a scenario mid-flow."""
    async with database.session() as session:
        return await MissionRepository(session).update(
            mission.model_copy(update={"status": status})
        )


# --- Phase 4 fixtures -----------------------------------------------------------


TEST_PHONE = "+15550000123"  # reserved-style placeholder, not a real number
TEST_PHONE_2 = "+15550000456"

_DAYTIME_PROBE_ZONES = (
    "Asia/Singapore",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
    "Pacific/Auckland",
    "Asia/Dubai",
)


def daytime_region() -> str:
    """An IANA zone where it is currently between 09:00 and 21:00 local time, so
    tests that run against the real clock never trip the quiet-hours gate.
    Tests of the gate itself pass an explicit region and ``now``."""
    from zoneinfo import ZoneInfo

    from callswarm.models import utcnow

    now = utcnow()
    for name in _DAYTIME_PROBE_ZONES:
        hour = now.astimezone(ZoneInfo(name)).hour
        if 9 <= hour < 21:
            return name
    raise AssertionError("no probe zone is in daytime; extend _DAYTIME_PROBE_ZONES")


@pytest.fixture
async def call_gate(database: Database, emitter: ActivityEventEmitter) -> CallGate:
    return CallGate(database, emitter)


@pytest.fixture
async def fake_provider(
    call_gate: CallGate, settings: Settings, database: Database
) -> FakeCallProvider:
    return FakeCallProvider(call_gate, settings, database)


@pytest.fixture
async def approval_service(
    database: Database,
    emitter: ActivityEventEmitter,
    settings: Settings,
    state_machine: MissionStateMachine,
) -> ApprovalService:
    return ApprovalService(database, emitter, settings, state_machine)


@pytest.fixture
async def call_service(
    fake_provider: FakeCallProvider,
    call_gate: CallGate,
    approval_service: ApprovalService,
    database: Database,
    emitter: ActivityEventEmitter,
    settings: Settings,
    state_machine: MissionStateMachine,
) -> CallService:
    return CallService(
        fake_provider, call_gate, approval_service, database, emitter, settings, state_machine
    )


@pytest.fixture
async def call_mission(database: Database) -> Mission:
    """A mission that permits up to three calls, positioned at CALL_AUTHORIZED."""
    async with database.session() as session:
        return await MissionRepository(session).add(
            Mission(
                user_goal="test goal",
                status=MissionStatus.CALL_AUTHORIZED,
                authority_policy=AuthorityPolicy(calls_allowed=True, max_call_count=3),
                call_budget=CallBudget(max_calls=3),
            )
        )


def make_intent(
    mission_id: str,
    *,
    phone: str = TEST_PHONE,
    region: str | None = "DAYTIME",
    entity_id: str | None = None,
    result_schema: dict[str, Any] | None = None,
    purpose: str = "confirm an open question",
) -> CallIntent:
    if region == "DAYTIME":
        region = daytime_region()
    return CallIntent(
        mission_id=mission_id,
        recipients=[CallRecipient(phone_e164=phone, region=region, entity_id=entity_id)],
        purpose=purpose,
        call_goal="Ask the recipient the listed questions on behalf of the user.",
        authorization_state=CallAuthorizationState.PENDING,
        result_schema=result_schema or {},
    )


async def persist_intent(database: Database, intent: CallIntent) -> CallIntent:
    async with database.session() as session:
        return await CallIntentRepository(session).add(intent)
