"""ASGI hook applying the shared sanitizer to every JSON response.

Bodies of ``application/json`` responses are buffered, walked and masked
before the first byte leaves the server. A reasoning-leak marker turns the
response into a 500 with a fixed, secret-free body. Non-JSON responses
(``text/event-stream`` in particular) pass through untouched — the event
emitter sanitizes those at persistence time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from callswarm.sanitize import ReasoningLeakError, Sanitizer

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REJECTED_BODY = b'{"detail":"response rejected by output sanitizer"}'


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    for name, value in headers:
        if name.lower() == b"content-type":
            return value.lower().startswith(b"application/json")
    return False


class SanitizingJSONMiddleware:
    def __init__(self, app: ASGIApp, sanitizer: Sanitizer) -> None:
        self.app = app
        self.sanitizer = sanitizer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_message: Message | None = None
        chunks: list[bytes] = []
        buffering = False

        async def send_wrapper(message: Message) -> None:
            nonlocal start_message, buffering
            if message["type"] == "http.response.start":
                if _is_json(list(message.get("headers", []))):
                    start_message = message
                    buffering = True
                    return
                await send(message)
                return
            if message["type"] == "http.response.body" and buffering:
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                assert start_message is not None
                await self._flush(start_message, b"".join(chunks), send)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _flush(self, start: Message, body: bytes, send: Send) -> None:
        status = int(start["status"])
        headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
        try:
            clean_body = self._sanitize_body(body)
        except ReasoningLeakError as exc:
            logger.error("response rejected by sanitizer: %s", exc)
            status = 500
            clean_body = REJECTED_BODY
        headers.append((b"content-length", str(len(clean_body)).encode("ascii")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": clean_body, "more_body": False})

    def _sanitize_body(self, body: bytes) -> bytes:
        if not body:
            return body
        try:
            data = json.loads(body)
        except ValueError:
            # Not parseable JSON despite the content type: mask as text.
            return self.sanitizer.sanitize_text(
                body.decode("utf-8", errors="replace"), context="API response"
            ).encode("utf-8")
        clean = self.sanitizer.sanitize_value(data, context="API response")
        return json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
