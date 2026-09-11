"""ActivityEvent emitter: sanitize, persist, then publish.

Order matters and is enforced by construction: an event reaches a subscriber
only after its row has been committed, so no event can exist in the UI without
a persisted record. Reconnecting clients replay from the persisted log by
sequence.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from callswarm.models import ActivityEvent
from callswarm.persistence import ActivityEventRepository, Database
from callswarm.sanitize import Sanitizer

logger = logging.getLogger(__name__)


class ActivityEventEmitter:
    def __init__(self, database: Database, sanitizer: Sanitizer) -> None:
        self._database = database
        self._sanitizer = sanitizer
        self._subscribers: dict[str, set[asyncio.Queue[ActivityEvent]]] = defaultdict(set)

    def sanitize(self, event: ActivityEvent) -> ActivityEvent:
        context = f"ActivityEvent {event.event_type} for mission {event.mission_id}"
        return event.model_copy(
            update={
                "summary": self._sanitizer.sanitize_text(event.summary, context=context),
                "payload": self._sanitizer.sanitize_value(event.payload, context=context),
            }
        )

    async def emit(self, event: ActivityEvent) -> ActivityEvent:
        """Sanitize, persist (assigning ``sequence``), then publish. Returns the stored event."""
        clean = self.sanitize(event)
        async with self._database.session() as session:
            stored = await ActivityEventRepository(session).add(clean)
        self._publish(stored)
        return stored

    def _publish(self, event: ActivityEvent) -> None:
        for queue in tuple(self._subscribers.get(event.mission_id, ())):
            queue.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self, mission_id: str) -> AsyncIterator[asyncio.Queue[ActivityEvent]]:
        queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
        self._subscribers[mission_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[mission_id].discard(queue)
            if not self._subscribers[mission_id]:
                del self._subscribers[mission_id]

    def subscriber_count(self, mission_id: str) -> int:
        return len(self._subscribers.get(mission_id, ()))

    async def replay(self, mission_id: str, *, after_sequence: int = 0) -> list[ActivityEvent]:
        async with self._database.session() as session:
            return await ActivityEventRepository(session).list_by_mission(
                mission_id, after_sequence=after_sequence
            )
