"""CS-005: persisted activity events, SSE replay and the shared sanitizer."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from callswarm.events import ActivityEventEmitter
from callswarm.models import ActivityEvent, ActivityEventType, Mission
from callswarm.persistence import ActivityEventRepository, Database, MissionRepository
from callswarm.persistence import repositories as repositories_module
from callswarm.sanitize import ReasoningLeakError, Sanitizer, mask_phones
from tests.sse_harness import read_sse

TEST_PHONE = "+15550000123"
TEST_PHONE_IN = "+919876543210"


def _event(mission_id: str, summary: str, **payload: Any) -> ActivityEvent:
    return ActivityEvent(
        mission_id=mission_id, event_type=ActivityEventType.SYSTEM, summary=summary, payload=payload
    )


# --- sanitizer unit behaviour -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (TEST_PHONE, "+1 ••••• ••123"),
        (TEST_PHONE_IN, "+91 ••••• ••210"),
        ("+91 98765 43210", "+91 ••••• ••210"),
        ("+44-20-7946-0958", "+44 ••••• ••958"),
        (f"call {TEST_PHONE} then {TEST_PHONE_IN}.", "call +1 ••••• ••123 then +91 ••••• ••210."),
        ("price 120000 INR", "price 120000 INR"),
        ("+1 short", "+1 short"),
    ],
)
def test_mask_phones(text: str, expected: str) -> None:
    assert mask_phones(text) == expected
    assert mask_phones(mask_phones(text)) == mask_phones(text)  # idempotent


def test_sanitizer_rejects_configured_markers_case_insensitively() -> None:
    s = Sanitizer(["<thinking>", "Chain of Thought"])
    with pytest.raises(ReasoningLeakError) as info:
        s.sanitize_text("Result. <THINKING>hmm", context="unit")
    assert info.value.marker == "<thinking>"
    with pytest.raises(ReasoningLeakError):
        s.sanitize_value({"a": ["fine", {"b": "my chain of thought is"}]}, context="unit")
    assert s.sanitize_text("Let me think", context="unit") == "Let me think"  # not configured


def test_sanitizer_walks_nested_values() -> None:
    s = Sanitizer()
    out = s.sanitize_value({"k": [TEST_PHONE, {TEST_PHONE: 1}], "n": 2, "b": None}, context="u")
    assert out == {"k": ["+1 ••••• ••123", {"+1 ••••• ••123": 1}], "n": 2, "b": None}


# --- emitter --------------------------------------------------------------------


async def test_emit_persists_before_publishing_and_assigns_sequence(
    emitter: ActivityEventEmitter, database: Database, mission: Mission
) -> None:
    async with emitter.subscribe(mission.id) as queue:
        stored = await emitter.emit(_event(mission.id, "first"))
        delivered = queue.get_nowait()
    assert stored.sequence == 1
    assert delivered == stored
    async with database.session() as s:
        rows = await ActivityEventRepository(s).list_by_mission(mission.id)
    assert rows == [stored]


async def test_emit_masks_and_rejects(
    emitter: ActivityEventEmitter, database: Database, mission: Mission
) -> None:
    stored = await emitter.emit(_event(mission.id, f"Dialing {TEST_PHONE}", recipient=TEST_PHONE))
    assert stored.summary == "Dialing +1 ••••• ••123"
    assert stored.payload == {"recipient": "+1 ••••• ••123"}
    async with emitter.subscribe(mission.id) as queue:
        with pytest.raises(ReasoningLeakError):
            await emitter.emit(_event(mission.id, "<thinking>private</thinking> done"))
        assert queue.empty()
    async with database.session() as s:
        rows = await ActivityEventRepository(s).list_by_mission(mission.id)
    assert [r.summary for r in rows] == ["Dialing +1 ••••• ••123"]


async def test_no_publish_without_persisted_row(
    emitter: ActivityEventEmitter, mission: Mission, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(self: Any, model: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(repositories_module.ActivityEventRepository, "add", _boom)
    async with emitter.subscribe(mission.id) as queue:
        with pytest.raises(RuntimeError, match="database down"):
            await emitter.emit(_event(mission.id, "lost"))
        assert queue.empty()


async def test_subscribers_are_per_mission_and_ordered(
    emitter: ActivityEventEmitter, database: Database, mission: Mission
) -> None:
    async with database.session() as s:
        other = await MissionRepository(s).add(Mission(user_goal="other"))
    async with emitter.subscribe(mission.id) as mine, emitter.subscribe(other.id) as theirs:
        for i in range(3):
            await emitter.emit(_event(mission.id, f"e{i}"))
        await emitter.emit(_event(other.id, "o0"))
        got = [mine.get_nowait() for _ in range(3)]
        assert [e.summary for e in got] == ["e0", "e1", "e2"]
        assert [e.sequence for e in got] == sorted(e.sequence or 0 for e in got)
        assert theirs.get_nowait().summary == "o0"
        assert mine.empty()
    assert emitter.subscriber_count(mission.id) == 0


# --- SSE endpoint ---------------------------------------------------------------


async def _seed_mission(app: FastAPI, goal: str = "sse goal") -> Mission:
    db: Database = app.state.database
    async with db.session() as s:
        return await MissionRepository(s).add(Mission(user_goal=goal))


async def test_sse_streams_live_events_with_ids(app: FastAPI) -> None:
    mission = await _seed_mission(app)
    emitter: ActivityEventEmitter = app.state.emitter

    async def produce() -> None:
        await emitter.emit(_event(mission.id, "one"))
        await emitter.emit(_event(mission.id, f"two {TEST_PHONE}"))

    session = await read_sse(
        app, f"/api/missions/{mission.id}/events", expected_events=2, after_headers=produce
    )
    assert session.status == 200
    assert session.headers["content-type"].startswith("text/event-stream")
    assert [e.id for e in session.events] == ["1", "2"]
    assert [e.event for e in session.events] == ["SYSTEM", "SYSTEM"]
    assert session.events[1].json()["summary"] == "two +1 ••••• ••123"
    assert TEST_PHONE not in json.dumps([e.data for e in session.events])


async def test_sse_replays_after_disconnect_using_last_event_id(app: FastAPI) -> None:
    mission = await _seed_mission(app)
    emitter: ActivityEventEmitter = app.state.emitter
    for i in range(5):
        await emitter.emit(_event(mission.id, f"e{i}"))

    first = await read_sse(app, f"/api/missions/{mission.id}/events", expected_events=2)
    assert [e.id for e in first.events][:2] == ["1", "2"]
    last_seen = first.events[1].id or "0"
    assert emitter.subscriber_count(mission.id) == 0  # disconnect released the subscription

    async def produce_more() -> None:
        await emitter.emit(_event(mission.id, "e5"))

    second = await read_sse(
        app,
        f"/api/missions/{mission.id}/events",
        headers={"Last-Event-ID": last_seen},
        expected_events=4,
        after_headers=produce_more,
    )
    assert [e.id for e in second.events] == ["3", "4", "5", "6"]
    assert [e.json()["summary"] for e in second.events] == ["e2", "e3", "e4", "e5"]
    assert all(e.json()["event_type"] == "SYSTEM" for e in second.events)


async def test_sse_query_fallback_and_no_duplicates(app: FastAPI) -> None:
    mission = await _seed_mission(app)
    emitter: ActivityEventEmitter = app.state.emitter
    await emitter.emit(_event(mission.id, "a"))
    await emitter.emit(_event(mission.id, "b"))

    session = await read_sse(
        app, f"/api/missions/{mission.id}/events?last_event_id=1", expected_events=1
    )
    ids = [e.id for e in session.events]
    assert ids == ["2"]
    assert len(ids) == len(set(ids))


async def test_sse_unknown_mission_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/missions/does-not-exist/events")
    assert response.status_code == 404


async def test_sse_rejects_bad_last_event_id(client: AsyncClient, app: FastAPI) -> None:
    mission = await _seed_mission(app)
    response = await client.get(
        f"/api/missions/{mission.id}/events", headers={"Last-Event-ID": "abc"}
    )
    assert response.status_code == 400


# --- REST sanitizer hook ---------------------------------------------------------


async def _insert_raw_event(app: FastAPI, mission_id: str, summary: str) -> None:
    """Bypass the emitter to simulate a future code path that forgot to sanitize."""
    db: Database = app.state.database
    async with db.session() as s:
        await ActivityEventRepository(s).add(_event(mission_id, summary))


async def test_rest_log_is_masked_by_response_hook(client: AsyncClient, app: FastAPI) -> None:
    mission = await _seed_mission(app)
    await _insert_raw_event(app, mission.id, f"Quote from {TEST_PHONE_IN}")
    response = await client.get(f"/api/missions/{mission.id}/events/log")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["summary"] == "Quote from +91 ••••• ••210"
    assert TEST_PHONE_IN not in response.text
    assert int(response.headers["content-length"]) == len(response.content)


async def test_rest_response_with_reasoning_marker_is_rejected(
    client: AsyncClient, app: FastAPI
) -> None:
    mission = await _seed_mission(app)
    await _insert_raw_event(app, mission.id, "chain of thought: first I considered")
    response = await client.get(f"/api/missions/{mission.id}/events/log")
    assert response.status_code == 500
    assert response.json() == {"detail": "response rejected by output sanitizer"}
    assert "first I considered" not in response.text


async def test_hook_covers_any_json_route(client: AsyncClient, app: FastAPI) -> None:
    async def leaky() -> dict[str, Any]:
        return {"note": f"reach us on {TEST_PHONE}", "nested": [TEST_PHONE_IN]}

    app.add_api_route("/__test/leaky", leaky, methods=["GET"])
    response = await client.get("/__test/leaky")
    assert response.status_code == 200
    assert response.json() == {"note": "reach us on +1 ••••• ••123", "nested": ["+91 ••••• ••210"]}


async def test_events_log_pagination(client: AsyncClient, app: FastAPI) -> None:
    mission = await _seed_mission(app)
    emitter: ActivityEventEmitter = app.state.emitter
    for i in range(4):
        await emitter.emit(_event(mission.id, f"p{i}"))
    page = await client.get(f"/api/missions/{mission.id}/events/log", params={"limit": 2})
    assert [e["sequence"] for e in page.json()] == [1, 2]
    page = await client.get(
        f"/api/missions/{mission.id}/events/log", params={"limit": 2, "after_sequence": 2}
    )
    assert [e["sequence"] for e in page.json()] == [3, 4]


async def test_emitter_replay_helper_matches_log(
    emitter: ActivityEventEmitter, mission: Mission
) -> None:
    await emitter.emit(_event(mission.id, "x"))
    await emitter.emit(_event(mission.id, "y"))
    replayed = await emitter.replay(mission.id, after_sequence=1)
    assert [e.summary for e in replayed] == ["y"]
    await asyncio.sleep(0)
