"""Approval endpoints (CS-034).

``POST .../{approval_id}/decision`` takes a required typed body
``{"decision": "APPROVED" | "REJECTED"}``. Approve and reject are never
distinguishable by path alone, and an empty or malformed body is a 422 from
validation before any handler code runs — there is no default decision.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from callswarm.approvals import (
    ApprovalNotFound,
    ApprovalNotPending,
    ApprovalService,
    Decision,
)
from callswarm.models import Approval
from callswarm.orchestrator.state_machine import IllegalTransition

router = APIRouter(prefix="/api/missions/{mission_id}/approvals", tags=["approvals"])


def get_approval_service_dep(request: Request) -> ApprovalService:
    state = request.app.state
    return ApprovalService(state.database, state.emitter, state.settings, state.state_machine)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Decision
    decided_by: str = Field(default="user", min_length=1, max_length=120)


class ApprovalView(BaseModel):
    id: str
    mission_id: str
    subject_type: str
    subject_id: str
    status: str
    requested_at: str
    decided_at: str | None
    expires_at: str | None
    decided_by: str | None
    reason: str | None

    @classmethod
    def from_model(cls, approval: Approval) -> ApprovalView:
        return cls(
            id=approval.id,
            mission_id=approval.mission_id,
            subject_type=approval.subject_type.value,
            subject_id=approval.subject_id,
            status=approval.status.value,
            requested_at=approval.requested_at.isoformat(),
            decided_at=approval.decided_at.isoformat() if approval.decided_at else None,
            expires_at=approval.expires_at.isoformat() if approval.expires_at else None,
            decided_by=approval.decided_by,
            reason=approval.reason,
        )


@router.get("", response_model=list[ApprovalView])
async def list_approvals(
    mission_id: str, service: Annotated[ApprovalService, Depends(get_approval_service_dep)]
) -> list[ApprovalView]:
    return [ApprovalView.from_model(a) for a in await service.list_for_mission(mission_id)]


@router.post("/{approval_id}/decision", response_model=ApprovalView)
async def decide_approval(
    mission_id: str,
    approval_id: str,
    body: DecisionRequest,
    service: Annotated[ApprovalService, Depends(get_approval_service_dep)],
) -> ApprovalView:
    try:
        existing = await service.get(approval_id)
    except ApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail="approval not found") from exc
    if existing.mission_id != mission_id:
        raise HTTPException(status_code=404, detail="approval not found")
    try:
        updated = await service.decide(approval_id, body.decision, body.decided_by)
    except ApprovalNotPending as exc:
        raise HTTPException(
            status_code=409, detail=f"approval is {exc.approval.status.value}, not PENDING"
        ) from exc
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApprovalView.from_model(updated)
