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
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.models import Mission
from callswarm.persistence import Database, MissionRepository
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
