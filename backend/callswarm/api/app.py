"""FastAPI application factory.

Startup: build settings, open the database and create the schema, construct
the sanitizer and the event emitter, and lazily verify the Gemini model when
credentials exist. Nothing network-bound happens at import time.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from callswarm import __version__
from callswarm.api import events, health
from callswarm.api.sanitizer_middleware import SanitizingJSONMiddleware
from callswarm.config.settings import Settings, get_settings
from callswarm.events import ActivityEventEmitter
from callswarm.llm.gemini import GeminiProvider, ModelVerification
from callswarm.persistence import Database
from callswarm.sanitize import Sanitizer

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    sanitizer = Sanitizer(resolved.reasoning_leak_markers)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved.database_url.get_secret_value())
        await database.create_schema()
        app.state.settings = resolved
        app.state.database = database
        app.state.sanitizer = sanitizer
        app.state.emitter = ActivityEventEmitter(database, sanitizer)
        provider = GeminiProvider(resolved)
        app.state.llm_provider = provider
        app.state.llm_verification = ModelVerification(
            status=provider.verification.status, model=provider.model
        )
        if provider.configured:
            app.state.llm_verification = await provider.verify_model()
            if app.state.llm_verification.status != "verified":
                logger.error(
                    "STARTUP ERROR: Gemini model %r unavailable (%s)",
                    provider.model,
                    app.state.llm_verification.detail,
                )
        else:
            logger.warning("LLM not configured; reasoning provider unavailable")
        logger.info(
            "CallSwarm %s: call_provider=%s live_calls=%s research=%s db=%s",
            __version__,
            resolved.call_provider,
            resolved.calle_live_calls_enabled,
            resolved.research_provider,
            database.kind,
        )
        try:
            yield
        finally:
            await database.dispose()

    app = FastAPI(title="CallSwarm", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SanitizingJSONMiddleware, sanitizer=sanitizer)
    app.include_router(health.router)
    app.include_router(events.router)
    return app


app = create_app()
