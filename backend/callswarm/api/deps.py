"""FastAPI dependencies resolving app-scoped services."""

from __future__ import annotations

from fastapi import Request

from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.persistence import Database


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database_dep(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_emitter_dep(request: Request) -> ActivityEventEmitter:
    emitter: ActivityEventEmitter = request.app.state.emitter
    return emitter
