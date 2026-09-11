"""CS-001: settings defaults and the secret-free health endpoint."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from httpx import AsyncClient

from callswarm.api.app import create_app
from callswarm.config.settings import DEFAULT_REASONING_LEAK_MARKERS, Settings

pytestmark = pytest.mark.usefixtures("assert_env_is_safe")


def test_defaults_encode_default_off_posture() -> None:
    s = Settings(_env_file=None)
    assert s.calle_live_calls_enabled is False
    assert s.call_provider == "fake"
    assert s.research_provider == "fixture"
    assert s.call_allowed_recipients == []
    assert s.gemini_api_key is None
    assert s.calle_api_key is None
    assert s.calle_webhook_secret is None
    assert s.search_api_key is None
    assert s.llm_configured is False
    assert s.calle_configured is False
    assert s.gemini_model == "gemini-3.5-flash"
    assert s.google_genai_use_vertexai is False
    assert s.calle_api_base_url == "https://api.heycall-e.com"
    assert s.call_max_per_mission == 5
    assert s.call_quiet_hours_start == "21:00"
    assert s.call_quiet_hours_end == "09:00"
    assert s.database_kind == "sqlite"
    assert s.backend_host == "127.0.0.1"
    assert s.backend_port == 8000
    assert s.cors_allowed_origins == ["http://localhost:3000"]
    assert "<thinking>" in s.reasoning_leak_markers


def test_every_env_example_variable_is_a_setting() -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    names = [
        line.split("=", 1)[0].strip()
        for line in example.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    ]
    assert names, ".env.example must declare variables"
    fields = set(Settings.model_fields)
    missing = [n for n in names if n.lower() not in fields and n != "NEXT_PUBLIC_API_BASE_URL"]
    assert missing == [], f"variables without a setting: {missing}"


def test_csv_lists_parse_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_ALLOWED_RECIPIENTS", "+15550000001, +15550000002 ,")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://a.test,http://b.test")
    monkeypatch.setenv("REASONING_LEAK_MARKERS", "<thinking>,secret marker")
    s = Settings(_env_file=None)
    assert s.call_allowed_recipients == ["+15550000001", "+15550000002"]
    assert s.cors_allowed_origins == ["http://a.test", "http://b.test"]
    assert s.reasoning_leak_markers == ["<thinking>", "secret marker"]


def test_empty_marker_list_falls_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REASONING_LEAK_MARKERS", "")
    assert Settings(_env_file=None).reasoning_leak_markers == list(DEFAULT_REASONING_LEAK_MARKERS)
    assert Settings(_env_file=None, reasoning_leak_markers=[]).reasoning_leak_markers


def test_empty_secret_env_values_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("CALLE_API_KEY", "   ")
    s = Settings(_env_file=None)
    assert s.gemini_api_key is None
    assert s.calle_api_key is None


def test_missing_optional_keys_do_not_crash_startup(tmp_path: Path) -> None:
    s = Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    application = create_app(s)
    assert application.title == "CallSwarm"


def test_secrets_never_serialize() -> None:
    s = Settings(_env_file=None, gemini_api_key="gem-secret-value", calle_api_key="calle-secret")
    dumped = s.model_dump_json()
    assert "gem-secret-value" not in dumped
    assert "calle-secret" not in dumped
    assert "gem-secret-value" not in repr(s)


async def test_health_reports_capabilities_without_secrets(tmp_path: Path) -> None:
    # Supplying a Gemini key makes the app *want* to verify the model, which
    # must not reach the network: the socket block turns that into a clean
    # "unavailable" report rather than a crash.
    s = Settings(
        _env_file=None,
        gemini_api_key="AIza-test-gemini-secret-0001",
        calle_api_key="calle-test-secret-0002",
        calle_webhook_secret="webhook-shared-secret-0003",
        search_api_key="search-secret-0004",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'health.db'}",
    )
    assert len(s.secret_values()) == 5
    application = create_app(s)
    async with application.router.lifespan_context(application):
        from httpx import ASGITransport

        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://t"
        ) as http:
            response = await http.get("/health")
    assert response.status_code == 200
    body = response.json()
    serialized = json.dumps(body)
    for value in s.secret_values():
        assert value not in serialized
    assert str(tmp_path) not in serialized
    assert body["status"] == "ok"
    assert body["llm_configured"] is True
    assert body["llm_model"] == "gemini-3.5-flash"
    assert body["llm_model_status"] == "unavailable"  # blocked network, reported not crashed
    assert body["call_provider"] == "fake"
    assert body["live_calls_enabled"] is False
    assert body["calle_configured"] is True
    assert body["research_provider"] == "fixture"
    assert body["database_kind"] == "sqlite"
    assert set(body) == {
        "status",
        "version",
        "llm_configured",
        "llm_model",
        "llm_model_status",
        "llm_model_detail",
        "call_provider",
        "live_calls_enabled",
        "calle_configured",
        "research_provider",
        "research_provider_effective",
        "research_fallback_reason",
        "database_kind",
        "call_max_per_mission",
        "allowed_recipient_count",
    }


async def test_health_without_credentials_skips_model_check(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["llm_configured"] is False
    assert body["llm_model_status"] == "not_configured"
    assert body["live_calls_enabled"] is False
    assert body["call_provider"] == "fake"


def test_network_is_blocked_for_the_suite() -> None:
    with pytest.raises(RuntimeError, match="network access is blocked"):
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
