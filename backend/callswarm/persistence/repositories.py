"""Repositories: one per aggregate, mapping domain models to ORM rows.

Rows and domain models share field names, so mapping is generic: JSON columns
receive the JSON-mode dump of a field, other columns receive the Python value
with enums unwrapped. Anything not present as a column (for example
``CallRun.recipient_results``) is handled by the specific repository.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import JSON, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from callswarm.models import (
    ActivityEvent,
    AgentRequest,
    AgentRun,
    AgentSpec,
    Approval,
    CallIntent,
    CallRun,
    CandidateEntity,
    EvidenceClaim,
    InformationGap,
    Mission,
    MissionTransition,
    PlanOption,
    RecipientResult,
    ResearchArtifact,
    ScheduledJob,
    StrategyCandidate,
    SuppressionEntry,
)
from callswarm.models.base import IdentifiedModel
from callswarm.models.enums import ScheduledJobStatus
from callswarm.persistence.orm import (
    MISSION_SCOPED_TABLES,
    ActivityEventRow,
    AgentRequestRow,
    AgentRunRow,
    AgentSpecRow,
    ApprovalRow,
    Base,
    CallIntentRow,
    CallRunRow,
    CandidateEntityRow,
    EvidenceClaimRow,
    InformationGapRow,
    MissionRow,
    MissionTransitionRow,
    PlanOptionRow,
    RecipientResultRow,
    ResearchArtifactRow,
    ScheduledJobRow,
    StrategyCandidateRow,
    SuppressionEntryRow,
)
from callswarm.sanitize import Sanitizer

D = TypeVar("D", bound=IdentifiedModel)
R = TypeVar("R", bound=Base)


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class Repository(Generic[D, R]):
    """Generic CRUD over one aggregate table."""

    domain: ClassVar[type[IdentifiedModel]]
    row: ClassVar[type[Base]]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- mapping ----------------------------------------------------------
    @classmethod
    def _column_names(cls) -> list[str]:
        return [column.name for column in cls.row.__table__.columns]

    @classmethod
    def _json_columns(cls) -> set[str]:
        return {
            column.name for column in cls.row.__table__.columns if isinstance(column.type, JSON)
        }

    def _to_values(self, model: D) -> dict[str, Any]:
        json_dump = model.model_dump(mode="json")
        py_dump = model.model_dump(mode="python")
        json_columns = self._json_columns()
        values: dict[str, Any] = {}
        for name in self._column_names():
            if name not in json_dump:
                continue
            values[name] = json_dump[name] if name in json_columns else _plain(py_dump[name])
        return values

    def _from_row(self, row: R) -> D:
        field_names = type(self).domain.model_fields
        data = {name: getattr(row, name) for name in self._column_names() if name in field_names}
        return type(self).domain.model_validate(data)  # type: ignore[return-value]

    def _new_row(self, model: D) -> R:
        return type(self).row(**self._to_values(model))  # type: ignore[return-value]

    # --- CRUD -------------------------------------------------------------
    async def add(self, model: D) -> D:
        row = self._new_row(model)
        self.session.add(row)
        await self.session.flush()
        return self._from_row(row)

    async def get(self, id_: str) -> D | None:
        row = await self.session.get(type(self).row, id_)
        return None if row is None else self._from_row(row)  # type: ignore[arg-type]

    async def list_by_mission(self, mission_id: str) -> list[D]:
        stmt = select(type(self).row).where(type(self).row.__table__.c.mission_id == mission_id)
        result = await self.session.execute(stmt)
        return [self._from_row(row) for row in result.scalars()]  # type: ignore[arg-type]

    async def update(self, model: D) -> D:
        row = await self.session.get(type(self).row, model.id)
        if row is None:
            raise KeyError(f"{type(self).domain.__name__} {model.id!r} not found")
        for name, value in self._to_values(model).items():
            setattr(row, name, value)
        await self.session.flush()
        return self._from_row(row)  # type: ignore[arg-type]

    async def delete(self, id_: str) -> bool:
        row = await self.session.get(type(self).row, id_)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True


class MissionRepository(Repository[Mission, MissionRow]):
    domain = Mission
    row = MissionRow

    async def list_all(self) -> list[Mission]:
        result = await self.session.execute(select(MissionRow).order_by(MissionRow.created_at))
        return [self._from_row(row) for row in result.scalars()]

    async def delete_cascade(self, mission_id: str) -> bool:
        """Delete a mission and every mission-scoped child.

        Suppression entries are not touched: ``SuppressionEntryRow`` is not in
        ``MISSION_SCOPED_TABLES`` and carries no mission foreign key.
        """
        row = await self.session.get(MissionRow, mission_id)
        if row is None:
            return False
        for table in MISSION_SCOPED_TABLES:
            await self.session.execute(
                delete(table).where(table.__table__.c.mission_id == mission_id)
            )
        await self.session.delete(row)
        await self.session.flush()
        return True


class MissionTransitionRepository(Repository[MissionTransition, MissionTransitionRow]):
    """Append-only: transitions are history and are never edited."""

    domain = MissionTransition
    row = MissionTransitionRow

    async def list_by_mission(self, mission_id: str) -> list[MissionTransition]:
        result = await self.session.execute(
            select(MissionTransitionRow)
            .where(MissionTransitionRow.mission_id == mission_id)
            .order_by(MissionTransitionRow.created_at, MissionTransitionRow.id)
        )
        return [self._from_row(row) for row in result.scalars()]

    async def update(self, model: MissionTransition) -> MissionTransition:
        raise NotImplementedError("mission transitions are append-only")

    async def delete(self, id_: str) -> bool:
        raise NotImplementedError("mission transitions are append-only")


class StrategyCandidateRepository(Repository[StrategyCandidate, StrategyCandidateRow]):
    domain = StrategyCandidate
    row = StrategyCandidateRow


class AgentSpecRepository(Repository[AgentSpec, AgentSpecRow]):
    domain = AgentSpec
    row = AgentSpecRow


class AgentRequestRepository(Repository[AgentRequest, AgentRequestRow]):
    domain = AgentRequest
    row = AgentRequestRow


class AgentRunRepository(Repository[AgentRun, AgentRunRow]):
    """Sanitizes model-authored text before it is ever written."""

    domain = AgentRun
    row = AgentRunRow

    def __init__(self, session: AsyncSession, sanitizer: Sanitizer | None = None) -> None:
        super().__init__(session)
        self.sanitizer = sanitizer or Sanitizer()

    def _sanitized(self, model: AgentRun) -> AgentRun:
        context = f"AgentRun {model.id} output"
        artifact = (
            None
            if model.output_artifact is None
            else self.sanitizer.sanitize_value(model.output_artifact, context=context)
        )
        summary = self.sanitizer.sanitize_text(model.activity_summary, context=context)
        return model.model_copy(update={"output_artifact": artifact, "activity_summary": summary})

    async def add(self, model: AgentRun) -> AgentRun:
        return await super().add(self._sanitized(model))

    async def update(self, model: AgentRun) -> AgentRun:
        return await super().update(self._sanitized(model))

    async def list_by_agent(self, agent_id: str) -> list[AgentRun]:
        result = await self.session.execute(
            select(AgentRunRow).where(AgentRunRow.agent_id == agent_id)
        )
        return [self._from_row(row) for row in result.scalars()]


class ResearchArtifactRepository(Repository[ResearchArtifact, ResearchArtifactRow]):
    domain = ResearchArtifact
    row = ResearchArtifactRow


class CandidateEntityRepository(Repository[CandidateEntity, CandidateEntityRow]):
    domain = CandidateEntity
    row = CandidateEntityRow


class InformationGapRepository(Repository[InformationGap, InformationGapRow]):
    domain = InformationGap
    row = InformationGapRow


class CallIntentRepository(Repository[CallIntent, CallIntentRow]):
    domain = CallIntent
    row = CallIntentRow


class RecipientResultRepository(Repository[RecipientResult, RecipientResultRow]):
    domain = RecipientResult
    row = RecipientResultRow

    async def add_for_run(self, run: CallRun, result: RecipientResult) -> RecipientResult:
        values = self._to_values(result.model_copy(update={"call_run_id": run.id}))
        values["mission_id"] = run.mission_id
        row = RecipientResultRow(**values)
        self.session.add(row)
        await self.session.flush()
        return self._from_row(row)

    async def list_by_run(self, call_run_id: str) -> list[RecipientResult]:
        result = await self.session.execute(
            select(RecipientResultRow)
            .where(RecipientResultRow.call_run_id == call_run_id)
            .order_by(RecipientResultRow.recipient_ref)
        )
        return [self._from_row(row) for row in result.scalars()]

    async def delete_by_run(self, call_run_id: str) -> None:
        await self.session.execute(
            delete(RecipientResultRow).where(RecipientResultRow.call_run_id == call_run_id)
        )


class CallRunRepository(Repository[CallRun, CallRunRow]):
    """Persists a run together with its per-recipient results."""

    domain = CallRun
    row = CallRunRow

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._recipients = RecipientResultRepository(session)

    async def _with_recipients(self, run: CallRun) -> CallRun:
        results = await self._recipients.list_by_run(run.id)
        return run.model_copy(update={"recipient_results": results})

    async def add(self, model: CallRun) -> CallRun:
        stored = await super().add(model)
        for result in model.recipient_results:
            await self._recipients.add_for_run(stored, result)
        return await self._with_recipients(stored)

    async def get(self, id_: str) -> CallRun | None:
        run = await super().get(id_)
        return None if run is None else await self._with_recipients(run)

    async def get_by_calle_call_id(self, calle_call_id: str) -> CallRun | None:
        result = await self.session.execute(
            select(CallRunRow).where(CallRunRow.calle_call_id == calle_call_id)
        )
        row = result.scalar_one_or_none()
        return None if row is None else await self._with_recipients(self._from_row(row))

    async def list_by_mission(self, mission_id: str) -> list[CallRun]:
        runs = await super().list_by_mission(mission_id)
        return [await self._with_recipients(run) for run in runs]

    async def update(self, model: CallRun) -> CallRun:
        stored = await super().update(model)
        await self._recipients.delete_by_run(model.id)
        for result in model.recipient_results:
            await self._recipients.add_for_run(stored, result)
        return await self._with_recipients(stored)


class EvidenceClaimRepository(Repository[EvidenceClaim, EvidenceClaimRow]):
    domain = EvidenceClaim
    row = EvidenceClaimRow


class PlanOptionRepository(Repository[PlanOption, PlanOptionRow]):
    domain = PlanOption
    row = PlanOptionRow


class ApprovalRepository(Repository[Approval, ApprovalRow]):
    domain = Approval
    row = ApprovalRow

    async def list_by_subject(self, subject_id: str) -> list[Approval]:
        result = await self.session.execute(
            select(ApprovalRow).where(ApprovalRow.subject_id == subject_id)
        )
        return [self._from_row(row) for row in result.scalars()]


class ActivityEventRepository(Repository[ActivityEvent, ActivityEventRow]):
    """Append-only log keyed by an autoincrement ``sequence``."""

    domain = ActivityEvent
    row = ActivityEventRow

    def _to_values(self, model: ActivityEvent) -> dict[str, Any]:
        values = super()._to_values(model)
        if values.get("sequence") is None:
            values.pop("sequence", None)
        return values

    async def list_by_mission(
        self, mission_id: str, *, after_sequence: int = 0, limit: int | None = None
    ) -> list[ActivityEvent]:
        stmt = (
            select(ActivityEventRow)
            .where(ActivityEventRow.mission_id == mission_id)
            .where(ActivityEventRow.sequence > after_sequence)
            .order_by(ActivityEventRow.sequence)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return [self._from_row(row) for row in result.scalars()]

    async def get(self, id_: str) -> ActivityEvent | None:
        result = await self.session.execute(
            select(ActivityEventRow).where(ActivityEventRow.id == id_)
        )
        row = result.scalar_one_or_none()
        return None if row is None else self._from_row(row)

    async def update(self, model: ActivityEvent) -> ActivityEvent:
        raise NotImplementedError("activity events are append-only")

    async def delete(self, id_: str) -> bool:
        raise NotImplementedError("activity events are append-only")


class SuppressionEntryRepository(Repository[SuppressionEntry, SuppressionEntryRow]):
    domain = SuppressionEntry
    row = SuppressionEntryRow

    async def list_by_mission(self, mission_id: str) -> list[SuppressionEntry]:
        raise NotImplementedError("suppression entries are mission-independent")

    async def list_all(self) -> list[SuppressionEntry]:
        result = await self.session.execute(select(SuppressionEntryRow))
        return [self._from_row(row) for row in result.scalars()]

    async def find_by_hash(self, phone_hash: str) -> list[SuppressionEntry]:
        result = await self.session.execute(
            select(SuppressionEntryRow).where(SuppressionEntryRow.phone_hash == phone_hash)
        )
        return [self._from_row(row) for row in result.scalars()]

    async def is_suppressed(self, phone_hash: str) -> bool:
        return bool(await self.find_by_hash(phone_hash))


class ScheduledJobRepository(Repository[ScheduledJob, ScheduledJobRow]):
    domain = ScheduledJob
    row = ScheduledJobRow

    async def list_due(
        self, now: datetime, statuses: Sequence[ScheduledJobStatus] = (ScheduledJobStatus.PENDING,)
    ) -> list[ScheduledJob]:
        result = await self.session.execute(
            select(ScheduledJobRow)
            .where(ScheduledJobRow.due_at <= now)
            .where(ScheduledJobRow.status.in_([s.value for s in statuses]))
            .order_by(ScheduledJobRow.due_at)
        )
        return [self._from_row(row) for row in result.scalars()]
