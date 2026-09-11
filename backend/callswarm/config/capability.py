"""Capability report: what this instance can do, with no secret values."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from callswarm.config.settings import Settings

LLMModelStatus = Literal["not_configured", "unverified", "verified", "unavailable"]


class CapabilityReport(BaseModel):
    """Secret-free description of the running configuration."""

    status: Literal["ok"] = "ok"
    version: str
    llm_configured: bool
    llm_model: str
    llm_model_status: LLMModelStatus
    llm_model_detail: str | None = None
    call_provider: str
    live_calls_enabled: bool
    calle_configured: bool
    research_provider: str
    database_kind: str
    call_max_per_mission: int
    allowed_recipient_count: int = Field(
        description="Count only; recipient numbers are never reported."
    )


def build_capability_report(
    settings: Settings,
    *,
    version: str,
    llm_model_status: LLMModelStatus,
    llm_model_detail: str | None = None,
) -> CapabilityReport:
    return CapabilityReport(
        version=version,
        llm_configured=settings.llm_configured,
        llm_model=settings.gemini_model,
        llm_model_status=llm_model_status,
        llm_model_detail=llm_model_detail,
        call_provider=settings.call_provider,
        live_calls_enabled=settings.calle_live_calls_enabled,
        calle_configured=settings.calle_configured,
        research_provider=settings.research_provider,
        database_kind=settings.database_kind,
        call_max_per_mission=settings.call_max_per_mission,
        allowed_recipient_count=len(settings.call_allowed_recipients),
    )
