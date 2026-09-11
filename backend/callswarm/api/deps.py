"""FastAPI dependencies resolving app-scoped services."""

from __future__ import annotations

from fastapi import Request

from callswarm.approvals import ApprovalService
from callswarm.calls.service import CallService
from callswarm.config.settings import Settings
from callswarm.events import ActivityEventEmitter
from callswarm.llm import LLMProvider
from callswarm.orchestrator.intake import MissionIntake
from callswarm.orchestrator.state_machine import MissionStateMachine
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


def get_llm_provider_dep(request: Request) -> LLMProvider:
    provider: LLMProvider = request.app.state.llm_provider
    return provider


def get_state_machine_dep(request: Request) -> MissionStateMachine:
    machine: MissionStateMachine = request.app.state.state_machine
    return machine


def get_intake_dep(request: Request) -> MissionIntake:
    state = request.app.state
    return MissionIntake(
        state.database, state.emitter, state.llm_provider, state.settings, state.state_machine
    )


def get_call_service_dep(request: Request) -> CallService:
    state = request.app.state
    approvals = ApprovalService(state.database, state.emitter, state.settings, state.state_machine)
    return CallService(
        state.call_provider,
        state.call_gate,
        approvals,
        state.database,
        state.emitter,
        state.settings,
        state.state_machine,
    )
