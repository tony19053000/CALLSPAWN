"""Mission intake endpoints (CS-010) and constraint revision (CS-042)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from callswarm.api.deps import get_database_dep, get_intake_dep, get_revision_service_dep
from callswarm.llm import AgentOutputInvalid, LLMError
from callswarm.models import AuthorityPolicy, ConstraintChange
from callswarm.orchestrator.intake import (
    MissionIntake,
    MissionNotAwaitingAnswersError,
    MissionView,
    UnknownQuestionError,
)
from callswarm.orchestrator.revision import (
    RevisionNotAllowedError,
    RevisionResult,
    RevisionService,
)
from callswarm.persistence import Database, MissionRepository

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


class ConstraintChangeRequest(ConstraintChange):
    """Typed revision body: no free text. At least one of updates, locks,
    unlocks or preference_changes must be present."""

    @model_validator(mode="after")
    def _not_empty(self) -> ConstraintChangeRequest:
        if not (self.updates or self.locks or self.unlocks or self.preference_changes):
            raise ValueError("a constraint change must update, lock, unlock or re-weight something")
        return self


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


@router.post("/{mission_id}/constraints", response_model=RevisionResult)
async def revise_constraints(
    mission_id: str,
    body: ConstraintChangeRequest,
    database: Annotated[Database, Depends(get_database_dep)],
    revision: Annotated[RevisionService, Depends(get_revision_service_dep)],
) -> RevisionResult:
    async with database.session() as session:
        mission = await MissionRepository(session).get(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="mission not found")
    try:
        return await revision.apply(mission, ConstraintChange.model_validate(body.model_dump()))
    except RevisionNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
