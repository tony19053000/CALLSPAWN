"""GET /health — capability report with no secret values."""

from __future__ import annotations

from fastapi import APIRouter, Request

from callswarm import __version__
from callswarm.config.capability import CapabilityReport, build_capability_report
from callswarm.llm.gemini import ModelVerification

router = APIRouter()


@router.get("/health", response_model=CapabilityReport)
async def health(request: Request) -> CapabilityReport:
    verification: ModelVerification = request.app.state.llm_verification
    return build_capability_report(
        request.app.state.settings,
        version=__version__,
        llm_model_status=verification.status,
        llm_model_detail=verification.detail,
    )
