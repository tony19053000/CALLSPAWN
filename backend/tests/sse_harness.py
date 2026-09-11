"""Minimal ASGI driver for reading a server-sent event stream in tests.

``httpx.ASGITransport`` buffers the whole response, which never completes for
an SSE stream. This harness feeds the app a request, parses events as body
chunks arrive, and signals ``http.disconnect`` once enough have been read —
exactly what a browser closing the tab does.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from dataclasses import dataclass, field
from typing import Any

Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Any, Receive, Send], Coroutine[Any, Any, None]]


@dataclass
class SSEEvent:
    id: str | None
    event: str | None
    data: str

    def json(self) -> Any:
        return json.loads(self.data)


@dataclass
class SSESession:
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    events: list[SSEEvent] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)


def _parse_block(block: str) -> SSEEvent | None:
    event_id: str | None = None
    event_name: str | None = None
    data_lines: list[str] = []
    for line in block.split("\n"):
        if not line or line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "id":
            event_id = value
        elif name == "event":
            event_name = value
        elif name == "data":
            data_lines.append(value)
    if not data_lines and event_id is None and event_name is None:
        return None
    return SSEEvent(event_id, event_name, "\n".join(data_lines))


async def read_sse(
    app: ASGIApp,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    expected_events: int,
    wait_seconds: float = 5.0,
    after_headers: Callable[[], Awaitable[None]] | None = None,
) -> SSESession:
    """Connect, run ``after_headers`` once the stream is open, collect events, disconnect."""
    session = SSESession()
    disconnect = asyncio.Event()
    got_enough = asyncio.Event()
    headers_seen = asyncio.Event()
    buffer = ""

    raw_headers = [(b"host", b"testserver"), (b"accept", b"text/event-stream")]
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode(), v.encode()))
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": raw_headers,
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
    }

    async def receive() -> Message:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal buffer
        if message["type"] == "http.response.start":
            session.status = int(message["status"])
            session.headers = {k.decode(): v.decode() for k, v in message.get("headers", [])}
            headers_seen.set()
            return
        if message["type"] == "http.response.body":
            buffer += message.get("body", b"").decode("utf-8").replace("\r\n", "\n")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                if block.startswith(":"):
                    session.comments.append(block)
                    continue
                parsed = _parse_block(block)
                if parsed is not None:
                    session.events.append(parsed)
            if len(session.events) >= expected_events:
                got_enough.set()
            if not message.get("more_body", False):
                got_enough.set()

    task: asyncio.Task[None] = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(headers_seen.wait(), wait_seconds)
        if after_headers is not None:
            await after_headers()
        if expected_events > 0:
            await asyncio.wait_for(got_enough.wait(), wait_seconds)
    finally:
        disconnect.set()
        try:
            await asyncio.wait_for(task, wait_seconds)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
    return session
