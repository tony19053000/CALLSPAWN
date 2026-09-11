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
    call_provider_simulated: bool = Field(
        default=True, description="True unless a real call provider is active."
    )
    live_calls_enabled: bool
    calle_configured: bool
    research_provider: str
    research_provider_effective: str
    research_fallback_reason: str | None = None
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
    research_provider_effective: str | None = None,
    research_fallback_reason: str | None = None,
    call_provider_simulated: bool = True,
) -> CapabilityReport:
    return CapabilityReport(
        version=version,
        llm_configured=settings.llm_configured,
        llm_model=settings.gemini_model,
        llm_model_status=llm_model_status,
        llm_model_detail=llm_model_detail,
        call_provider=settings.call_provider,
        call_provider_simulated=call_provider_simulated,
        live_calls_enabled=settings.calle_live_calls_enabled,
        calle_configured=settings.calle_configured,
        research_provider=settings.research_provider,
        research_provider_effective=research_provider_effective or settings.research_provider,
        research_fallback_reason=research_fallback_reason,
        database_kind=settings.database_kind,
        call_max_per_mission=settings.call_max_per_mission,
        allowed_recipient_count=len(settings.call_allowed_recipients),
    )
