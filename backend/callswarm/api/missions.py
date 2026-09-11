"""Mission intake endpoints (CS-010)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from callswarm.api.deps import get_intake_dep
from callswarm.llm import AgentOutputInvalid, LLMError
from callswarm.models import AuthorityPolicy
from callswarm.orchestrator.intake import (
    MissionIntake,
    MissionNotAwaitingAnswersError,
    MissionView,
    UnknownQuestionError,
)

router = APIRouter(prefix="/api/missions", tags=["missions"])

MAX_GOAL_LENGTH = 4000
MAX_ANSWER_LENGTH = 2000


class CreateMissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=MAX_GOAL_LENGTH)
    authority_policy: AuthorityPolicy | None = Field(
        default=None,
        description=(
            "Permissions the user explicitly grants. Omitted means the default: research "
            "only, no calls. Free text in the goal never widens this."
        ),
    )


class AnswersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: dict[str, Annotated[str, Field(max_length=MAX_ANSWER_LENGTH)]] = Field(min_length=1)


def _llm_errors(exc: LLMError) -> HTTPException:
    """Any provider failure is reported plainly; the intake has already BLOCKED the mission."""
    if isinstance(exc, AgentOutputInvalid):
        return HTTPException(status_code=502, detail="reasoning provider returned invalid output")
    return HTTPException(status_code=503, detail="reasoning provider unavailable")


@router.post("", response_model=MissionView, status_code=201)
async def create_mission(
    body: CreateMissionRequest,
    request: Request,
    intake: Annotated[MissionIntake, Depends(get_intake_dep)],
) -> MissionView:
    try:
        return await intake.create_mission(body.goal, body.authority_policy)
    except LLMError as exc:
        raise _llm_errors(exc) from exc


@router.get("/{mission_id}", response_model=MissionView)
async def get_mission(
    mission_id: str, intake: Annotated[MissionIntake, Depends(get_intake_dep)]
) -> MissionView:
    try:
        return await intake.get_view(mission_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="mission not found") from exc


@router.post("/{mission_id}/answers", response_model=MissionView)
async def answer_questions(
    mission_id: str,
    body: AnswersRequest,
    intake: Annotated[MissionIntake, Depends(get_intake_dep)],
) -> MissionView:
    try:
        return await intake.answer_questions(mission_id, body.answers)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="mission not found") from exc
    except MissionNotAwaitingAnswersError as exc:
        raise HTTPException(
            status_code=409, detail=f"mission is {exc.status.value}; not awaiting answers"
        ) from exc
    except UnknownQuestionError as exc:
        raise HTTPException(
            status_code=400, detail=f"unknown question id(s): {', '.join(exc.question_ids)}"
        ) from exc
    except LLMError as exc:
        raise _llm_errors(exc) from exc
