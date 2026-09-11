"""Mission activity: SSE stream with replay, plus a paginated JSON log."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from callswarm.api.deps import get_database_dep, get_emitter_dep
from callswarm.events import ActivityEventEmitter
from callswarm.models import ActivityEvent
from callswarm.persistence import ActivityEventRepository, Database, MissionRepository

router = APIRouter(prefix="/api/missions", tags=["events"])

KEEPALIVE_SECONDS = 15.0


def _parse_last_event_id(request: Request, query_value: int | None) -> int:
    header = request.headers.get("last-event-id")
    if header is not None:
        try:
            return max(int(header), 0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
    return max(query_value or 0, 0)


def _sse_message(event: ActivityEvent) -> dict[str, Any]:
    assert event.sequence is not None
    return {
        "id": str(event.sequence),
        "event": event.event_type.value,
        "data": event.model_dump_json(),
    }


async def _mission_exists(database: Database, mission_id: str) -> None:
    async with database.session() as session:
        if await MissionRepository(session).get(mission_id) is None:
            raise HTTPException(status_code=404, detail="mission not found")


@router.get("/{mission_id}/events")
async def stream_events(
    mission_id: str,
    request: Request,
    emitter: Annotated[ActivityEventEmitter, Depends(get_emitter_dep)],
    database: Annotated[Database, Depends(get_database_dep)],
    last_event_id: Annotated[int | None, Query(ge=0)] = None,
) -> EventSourceResponse:
    await _mission_exists(database, mission_id)
    start_after = _parse_last_event_id(request, last_event_id)

    async def generate() -> AsyncIterator[dict[str, Any]]:
        last_sent = start_after
        # Subscribe before replaying so nothing emitted in between is lost.
        async with emitter.subscribe(mission_id) as queue:
            for event in await emitter.replay(mission_id, after_sequence=start_after):
                assert event.sequence is not None
                last_sent = event.sequence
                yield _sse_message(event)
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    continue
                if event.sequence is None or event.sequence <= last_sent:
                    continue
                last_sent = event.sequence
                yield _sse_message(event)

    return EventSourceResponse(generate(), ping=KEEPALIVE_SECONDS)


@router.get("/{mission_id}/events/log", response_model=list[ActivityEvent])
async def list_events(
    mission_id: str,
    database: Annotated[Database, Depends(get_database_dep)],
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ActivityEvent]:
    await _mission_exists(database, mission_id)
    async with database.session() as session:
        return await ActivityEventRepository(session).list_by_mission(
            mission_id, after_sequence=after_sequence, limit=limit
        )
