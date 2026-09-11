"""Evidence endpoints (CS-040): paginated claims and per-claim traces."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from callswarm.api.deps import get_database_dep, get_evidence_engine_dep
from callswarm.evidence import ClaimPage, EvidenceEngine, EvidenceTrace
from callswarm.models import EvidenceStatus
from callswarm.persistence import Database, MissionRepository

router = APIRouter(prefix="/api/missions", tags=["evidence"])

MAX_FILTER_LENGTH = 500


async def _mission_exists(database: Database, mission_id: str) -> None:
    async with database.session() as session:
        if await MissionRepository(session).get(mission_id) is None:
            raise HTTPException(status_code=404, detail="mission not found")


@router.get("/{mission_id}/evidence", response_model=ClaimPage)
async def list_evidence(
    mission_id: str,
    database: Annotated[Database, Depends(get_database_dep)],
    engine: Annotated[EvidenceEngine, Depends(get_evidence_engine_dep)],
    subject: Annotated[str | None, Query(max_length=MAX_FILTER_LENGTH)] = None,
    predicate: Annotated[str | None, Query(max_length=MAX_FILTER_LENGTH)] = None,
    status: EvidenceStatus | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ClaimPage:
    await _mission_exists(database, mission_id)
    return await engine.list_claims(
        mission_id,
        subject=subject,
        predicate=predicate,
        status=status,
        offset=offset,
        limit=limit,
    )


@router.get("/{mission_id}/evidence/{claim_id}/trace", response_model=EvidenceTrace)
async def trace_claim(
    mission_id: str,
    claim_id: str,
    database: Annotated[Database, Depends(get_database_dep)],
    engine: Annotated[EvidenceEngine, Depends(get_evidence_engine_dep)],
) -> EvidenceTrace:
    await _mission_exists(database, mission_id)
    claim = await engine.get_claim(mission_id, claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="claim not found")
    return await engine.trace(mission_id, claim.subject, claim.predicate)
