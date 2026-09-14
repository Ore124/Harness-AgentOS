"""Transactional event store, projections, leases, and outbox.

PostgreSQL is the production backend. SQLite uses the same schema for local
single-worker development and deterministic tests; it intentionally does not
claim distributed locking guarantees.
"""
from __future__ import annotations

import uuid
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Protocol, Sequence

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import StaticPool

from orchestrator.domain import (
    ConcurrencyError,
    DomainError,
    LeasedWorkItem,
    RunStatus,
    StaleLeaseError,
    WorkflowEvent,
    WorkItemKind,
    WorkItemStatus,
    utc_now,
)
from orchestrator.durable_reducer import apply_event, empty_aggregate


metadata = MetaData()

projects = Table(
    "projects",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("max_concurrent_runs", Integer, nullable=False, default=20),
    Column("max_active_work_items", Integer, nullable=False, default=200),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

memberships = Table(
    "project_memberships",
    metadata,
    Column("project_id", String(128), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("subject", String(255), primary_key=True),
    Column("role", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

runs = Table(
    "workflow_runs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("goal", Text, nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("current_plan_revision", Integer),
    Column("snapshot_json", JSON, nullable=False),
    Column("idempotency_key", String(255)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("project_id", "idempotency_key", name="uq_run_idempotency"),
)

workflow_events = Table(
    "workflow_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("run_id", String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload_json", JSON, nullable=False),
    Column("actor", String(255), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("run_id", "version", name="uq_workflow_event_version"),
)

plan_revisions = Table(
    "plan_revisions",
    metadata,
    Column("run_id", String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("plan_json", JSON, nullable=False),
    Column("reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

work_items = Table(
    "work_items",
    metadata,
    Column("run_id", String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), primary_key=True),
    Column("item_id", String(128), primary_key=True),
    Column("plan_revision", Integer, nullable=False),
    Column("item_json", JSON, nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("required_capabilities_json", JSON, nullable=False),
    Column("priority", Integer, nullable=False, default=0),
    Column("not_before", DateTime(timezone=True)),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=3),
    Column("fencing_token", Integer, nullable=False, default=0),
    Column("lease_owner", String(255)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("current_attempt_id", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index("ix_work_queue", work_items.c.status, work_items.c.not_before, work_items.c.priority)

attempts = Table(
    "work_attempts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("work_item_id", String(128), nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("worker_id", String(255), nullable=False),
    Column("fencing_token", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("lease_expires_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("outcome_json", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["run_id", "work_item_id"],
        ["work_items.run_id", "work_items.item_id"],
        ondelete="CASCADE",
    ),
    UniqueConstraint("run_id", "work_item_id", "attempt_number", name="uq_attempt_number"),
)

checkpoints = Table(
    "attempt_checkpoints",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("attempt_id", String(64), ForeignKey("work_attempts.id", ondelete="CASCADE"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("checkpoint_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("attempt_id", "sequence", name="uq_checkpoint_sequence"),
)

evidence = Table(
    "workflow_evidence",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
    Column("work_item_id", String(128)),
    Column("kind", String(64), nullable=False),
    Column("payload_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

artifacts = Table(
    "workflow_artifacts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
    Column("work_item_id", String(128)),
    Column("uri", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size", Integer, nullable=False),
    Column("media_type", String(255)),
    Column("sensitivity", String(32), nullable=False, default="internal"),
    Column("name", String(512)),
    Column("metadata_json", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

approvals = Table(
    "workflow_approvals",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
    Column("plan_revision", Integer, nullable=False),
    Column("effect_digest", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("requested_by", String(255), nullable=False),
    Column("decided_by", String(255)),
    Column("decision_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True)),
)

effect_ledger = Table(
    "effect_ledger",
    metadata,
    Column("effect_id", String(255), primary_key=True),
    Column("run_id", String(64), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
    Column("work_item_id", String(128), nullable=False),
    Column("attempt_id", String(64), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("result_json", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

outbox = Table(
    "workflow_outbox",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("event_id", String(64), ForeignKey("workflow_events.event_id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("run_id", String(64), nullable=False, index=True),
    Column("event_type", String(64), nullable=False),
    Column("payload_json", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True)),
)

workers = Table(
    "workers",
    metadata,
    Column("id", String(255), primary_key=True),
    Column("capabilities_json", JSON, nullable=False),
    Column("status", String(32), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
)

integration_locks = Table(
    "project_integration_locks",
    metadata,
    Column(
        "project_id",
        String(128),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("attempt_id", String(64)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class WorkflowRepository(Protocol):
    def append(self, run_id: str, expected_version: int, events: Sequence[WorkflowEvent]) -> dict[str, Any]: ...
    def claim_work(self, worker_id: str, capabilities: Iterable[str], limit: int = 1, run_id: str | None = None) -> list[LeasedWorkItem]: ...
    def renew_lease(self, attempt_id: str, fencing_token: int) -> str: ...
    def commit_checkpoint(self, attempt_id: str, fencing_token: int, checkpoint: Mapping[str, Any]) -> dict[str, Any]: ...
    def complete_attempt(self, attempt_id: str, fencing_token: int, outcome: Mapping[str, Any]) -> dict[str, Any]: ...


class SqlWorkflowRepository:
    LEASE_SECONDS = 60

    def __init__(
        self,
        database_url: str,
        *,
        create_schema: bool = True,
        retry_backoff_base_seconds: float = 0,
    ):
        options: dict[str, Any] = {"future": True}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            options.update(
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        elif database_url.startswith("sqlite:"):
            options.update(connect_args={"check_same_thread": False})
        self.engine: Engine = create_engine(database_url, **options)
        self._sqlite_claim_lock = threading.RLock()
        self.retry_backoff_base_seconds = max(0.0, retry_backoff_base_seconds)
        if create_schema:
            metadata.create_all(self.engine)

    def close(self) -> None:
        """Release pooled connections, primarily for tests and CLI shutdown."""
        self.engine.dispose()

    def create_project(self, project_id: str, name: str | None = None) -> None:
        now = _now()
        with self.engine.begin() as connection:
            connection.execute(
                _insert_do_nothing(
                    connection,
                    projects,
                    {
                        "id": project_id,
                        "name": name or project_id,
                        "max_concurrent_runs": 20,
                        "max_active_work_items": 200,
                        "created_at": now,
                    },
                    ["id"],
                )
            )
            connection.execute(
                _insert_do_nothing(
                    connection,
                    integration_locks,
                    {
                        "project_id": project_id,
                        "attempt_id": None,
                        "lease_expires_at": None,
                        "updated_at": now,
                    },
                    ["project_id"],
                )
            )

    def set_project_limits(
        self,
        project_id: str,
        *,
        max_concurrent_runs: int,
        max_active_work_items: int,
    ) -> None:
        if max_concurrent_runs < 1 or max_active_work_items < 1:
            raise DomainError("project limits must be positive")
        self.create_project(project_id)
        with self.engine.begin() as connection:
            connection.execute(
                update(projects)
                .where(projects.c.id == project_id)
                .values(
                    max_concurrent_runs=max_concurrent_runs,
                    max_active_work_items=max_active_work_items,
                )
            )

    def set_membership(self, project_id: str, subject: str, role: str) -> None:
        if role not in {"owner", "operator", "approver", "viewer"}:
            raise DomainError(f"unknown project role: {role}")
        self.create_project(project_id)
        now = _now()
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(memberships.c.subject).where(
                    memberships.c.project_id == project_id,
                    memberships.c.subject == subject,
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    insert(memberships).values(
                        project_id=project_id,
                        subject=subject,
                        role=role,
                        created_at=now,
                    )
                )
            else:
                connection.execute(
                    update(memberships)
                    .where(
                        memberships.c.project_id == project_id,
                        memberships.c.subject == subject,
                    )
                    .values(role=role)
                )

    def get_membership_role(self, project_id: str, subject: str) -> str | None:
        with self.engine.connect() as connection:
            return connection.execute(
                select(memberships.c.role).where(
                    memberships.c.project_id == project_id,
                    memberships.c.subject == subject,
                )
            ).scalar_one_or_none()

    def create_run(
        self,
        project_id: str,
        goal: str,
        *,
        policy: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        run_id: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        if not goal.strip():
            raise DomainError("run goal is required")
        self.create_project(project_id)
        with self.engine.begin() as connection:
            if idempotency_key:
                existing = connection.execute(
                    select(runs.c.snapshot_json).where(
                        runs.c.project_id == project_id,
                        runs.c.idempotency_key == idempotency_key,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return dict(existing)
            project_limit = connection.execute(
                select(projects.c.max_concurrent_runs)
                .where(projects.c.id == project_id)
                .with_for_update()
            ).scalar_one()
            if idempotency_key:
                existing = connection.execute(
                    select(runs.c.snapshot_json).where(
                        runs.c.project_id == project_id,
                        runs.c.idempotency_key == idempotency_key,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return dict(existing)
            active_runs = connection.execute(
                select(func.count())
                .select_from(runs)
                .where(
                    runs.c.project_id == project_id,
                    runs.c.status.in_(
                        [
                            RunStatus.QUEUED.value,
                            RunStatus.RUNNING.value,
                            RunStatus.PAUSED.value,
                            RunStatus.WAITING_APPROVAL.value,
                        ]
                    ),
                )
            ).scalar_one()
            if int(active_runs) >= int(project_limit):
                raise DomainError(
                    f"project {project_id} reached its concurrent run limit ({project_limit})"
                )
            resolved_run_id = run_id or str(uuid.uuid4())
            event = WorkflowEvent.create(
                resolved_run_id,
                "run_created",
                {"project_id": project_id, "goal": goal, "policy": dict(policy or {})},
                actor=actor,
            )
            event = replace(event, version=1)
            aggregate = apply_event(empty_aggregate(resolved_run_id), event)
            now = _parse_datetime(event.created_at)
            connection.execute(
                insert(runs).values(
                    id=resolved_run_id,
                    project_id=project_id,
                    goal=goal,
                    status=aggregate["status"],
                    version=aggregate["version"],
                    current_plan_revision=None,
                    snapshot_json=aggregate,
                    idempotency_key=idempotency_key,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._record_event(connection, event)
            return aggregate

    def append(
        self,
        run_id: str,
        expected_version: int,
        events_to_append: Sequence[WorkflowEvent],
    ) -> dict[str, Any]:
        if not events_to_append:
            return self.get_snapshot(run_id)
        with self.engine.begin() as connection:
            row = self._lock_run(connection, run_id)
            aggregate = dict(row.snapshot_json)
            if int(row.version) != expected_version:
                event_ids = [event.event_id for event in events_to_append]
                existing_count = connection.execute(
                    select(workflow_events.c.event_id).where(
                        workflow_events.c.event_id.in_(event_ids)
                    )
                ).all()
                if len(existing_count) == len(event_ids):
                    return aggregate
                raise ConcurrencyError(
                    f"run {run_id} expected version {expected_version}, current is {row.version}"
                )
            aggregate = self._append_locked(
                connection, row, aggregate, events_to_append
            )
            return aggregate

    def get_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(runs.c.snapshot_json).where(runs.c.id == run_id)
            ).scalar_one_or_none()
        if value is None:
            raise FileNotFoundError(run_id)
        return dict(value)

    def list_runs(self, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        statement = select(runs.c.snapshot_json).order_by(runs.c.updated_at.desc()).limit(limit)
        if project_id:
            statement = statement.where(runs.c.project_id == project_id)
        with self.engine.connect() as connection:
            return [dict(value) for value in connection.execute(statement).scalars()]

    def list_events(self, run_id: str, after_version: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        statement = (
            select(workflow_events)
            .where(
                workflow_events.c.run_id == run_id,
                workflow_events.c.version > after_version,
            )
            .order_by(workflow_events.c.version)
            .limit(max(1, min(limit, 1000)))
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._event_from_row(row).to_dict() for row in rows]

    def list_audit_events(
        self,
        project_id: str,
        *,
        after_created_at: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        statement = (
            select(workflow_events, runs.c.project_id)
            .join(runs, runs.c.id == workflow_events.c.run_id)
            .where(runs.c.project_id == project_id)
        )
        if after_created_at is not None:
            statement = statement.where(workflow_events.c.created_at > after_created_at)
        statement = statement.order_by(workflow_events.c.created_at, workflow_events.c.version).limit(
            max(1, min(limit, 1000))
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            {
                **self._event_from_row(row).to_dict(),
                "project_id": row["project_id"],
            }
            for row in rows
        ]

    def get_plan(self, run_id: str, revision: int | None = None) -> dict[str, Any]:
        if revision is None:
            snapshot = self.get_snapshot(run_id)
            if snapshot.get("plan") is None:
                raise FileNotFoundError(f"run {run_id} has no plan")
            return dict(snapshot["plan"])
        with self.engine.connect() as connection:
            value = connection.execute(
                select(plan_revisions.c.plan_json).where(
                    plan_revisions.c.run_id == run_id,
                    plan_revisions.c.revision == revision,
                )
            ).scalar_one_or_none()
        if value is None:
            raise FileNotFoundError(f"plan {run_id}/{revision}")
        return dict(value)

    def claim_work(
        self,
        worker_id: str,
        capabilities: Iterable[str],
        limit: int = 1,
        run_id: str | None = None,
    ) -> list[LeasedWorkItem]:
        if self.engine.dialect.name == "sqlite":
            with self._sqlite_claim_lock:
                return self._claim_work_unlocked(worker_id, capabilities, limit, run_id)
        return self._claim_work_unlocked(worker_id, capabilities, limit, run_id)

    def _claim_work_unlocked(
        self,
        worker_id: str,
        capabilities: Iterable[str],
        limit: int,
        run_id: str | None,
    ) -> list[LeasedWorkItem]:
        capabilities_set = {str(value) for value in capabilities}
        now = _now()
        lease_until = now + timedelta(seconds=self.LEASE_SECONDS)
        claimed: list[LeasedWorkItem] = []
        with self.engine.begin() as connection:
            self._upsert_worker(connection, worker_id, capabilities_set, now)
            statement = (
                select(
                    work_items,
                    runs.c.project_id,
                    runs.c.snapshot_json,
                    runs.c.version,
                )
                .join(runs, runs.c.id == work_items.c.run_id)
                .where(
                    work_items.c.status == WorkItemStatus.READY.value,
                    runs.c.status == RunStatus.RUNNING.value,
                    or_(work_items.c.not_before.is_(None), work_items.c.not_before <= now),
                )
                .order_by(work_items.c.priority.desc(), work_items.c.created_at)
                .limit(max(limit * 8, limit))
                .with_for_update(skip_locked=self.engine.dialect.name == "postgresql")
            )
            if run_id is not None:
                statement = statement.where(work_items.c.run_id == run_id)
            active_by_project: dict[str, int] = {}
            limit_by_project: dict[str, int] = {}
            for row in connection.execute(statement).mappings():
                required = set(row["required_capabilities_json"] or [])
                if not required.issubset(capabilities_set):
                    continue
                project_id = str(row["project_id"])
                if project_id not in active_by_project:
                    limit_by_project[project_id] = int(
                        connection.execute(
                            select(projects.c.max_active_work_items)
                            .where(projects.c.id == project_id)
                            .with_for_update()
                        ).scalar_one()
                    )
                    active_by_project[project_id] = int(
                        connection.execute(
                            select(func.count())
                            .select_from(work_items.join(runs, runs.c.id == work_items.c.run_id))
                            .where(
                                runs.c.project_id == project_id,
                                work_items.c.status.in_(
                                    [
                                        WorkItemStatus.LEASED.value,
                                        WorkItemStatus.RUNNING.value,
                                        WorkItemStatus.VERIFYING.value,
                                    ]
                                ),
                            )
                        ).scalar_one()
                    )
                if active_by_project[project_id] >= limit_by_project[project_id]:
                    continue
                is_integration = (
                    (row["item_json"] or {}).get("kind")
                    == WorkItemKind.INTEGRATE.value
                )
                if is_integration:
                    integration_lock = connection.execute(
                        select(integration_locks)
                        .where(integration_locks.c.project_id == project_id)
                        .with_for_update()
                    ).mappings().one()
                    if (
                        integration_lock["attempt_id"]
                        and integration_lock["lease_expires_at"]
                        and _as_utc(integration_lock["lease_expires_at"]) > now
                    ):
                        continue
                attempt_id = str(uuid.uuid4())
                attempt_number = int(row["attempt_count"]) + 1
                fencing_token = int(row["fencing_token"]) + 1
                updated = connection.execute(
                    update(work_items)
                    .where(
                        work_items.c.run_id == row["run_id"],
                        work_items.c.item_id == row["item_id"],
                        work_items.c.status == WorkItemStatus.READY.value,
                        work_items.c.fencing_token == row["fencing_token"],
                    )
                    .values(
                        status=WorkItemStatus.LEASED.value,
                        attempt_count=attempt_number,
                        fencing_token=fencing_token,
                        lease_owner=worker_id,
                        lease_expires_at=lease_until,
                        current_attempt_id=attempt_id,
                        updated_at=now,
                    )
                )
                if updated.rowcount != 1:
                    continue
                connection.execute(
                    insert(attempts).values(
                        id=attempt_id,
                        run_id=row["run_id"],
                        work_item_id=row["item_id"],
                        attempt_number=attempt_number,
                        worker_id=worker_id,
                        fencing_token=fencing_token,
                        status=WorkItemStatus.LEASED.value,
                        lease_expires_at=lease_until,
                        created_at=now,
                    )
                )
                if is_integration:
                    connection.execute(
                        update(integration_locks)
                        .where(integration_locks.c.project_id == project_id)
                        .values(
                            attempt_id=attempt_id,
                            lease_expires_at=lease_until,
                            updated_at=now,
                        )
                    )
                run_row = self._lock_run(connection, row["run_id"])
                aggregate = dict(run_row.snapshot_json)
                event = WorkflowEvent.create(
                    row["run_id"],
                    "work_leased",
                    {"work_item_id": row["item_id"], "attempt_id": attempt_id},
                    actor=f"worker:{worker_id}",
                )
                self._append_locked(connection, run_row, aggregate, [event], project=False)
                claimed.append(
                    LeasedWorkItem(
                        run_id=row["run_id"],
                        work_item_id=row["item_id"],
                        attempt_id=attempt_id,
                        fencing_token=fencing_token,
                        lease_expires_at=lease_until.isoformat(),
                        item=dict(row["item_json"]),
                        checkpoint=self._latest_checkpoint_for_item(
                            connection, row["run_id"], row["item_id"]
                        ),
                    )
                )
                active_by_project[project_id] += 1
                if len(claimed) >= limit:
                    break
        return claimed

    def start_attempt(self, attempt_id: str, fencing_token: int) -> dict[str, Any]:
        now = _now()
        with self.engine.begin() as connection:
            attempt, item = self._lock_attempt(connection, attempt_id, fencing_token)
            if attempt.status in {WorkItemStatus.RUNNING.value, WorkItemStatus.VERIFYING.value}:
                return self._snapshot_locked(connection, attempt.run_id)
            if attempt.status != WorkItemStatus.LEASED.value:
                raise StaleLeaseError(f"attempt {attempt_id} is not leased")
            if _as_utc(attempt.lease_expires_at) <= now:
                raise StaleLeaseError(f"attempt {attempt_id} lease expired")
            item_kind = (item.item_json or {}).get("kind")
            target = (
                WorkItemStatus.VERIFYING.value
                if item_kind == "verify"
                else WorkItemStatus.RUNNING.value
            )
            connection.execute(
                update(attempts).where(attempts.c.id == attempt_id).values(
                    status=target, started_at=now
                )
            )
            connection.execute(
                update(work_items)
                .where(
                    work_items.c.run_id == attempt.run_id,
                    work_items.c.item_id == attempt.work_item_id,
                )
                .values(status=target, updated_at=now)
            )
            run_row = self._lock_run(connection, attempt.run_id)
            event = WorkflowEvent.create(
                attempt.run_id,
                "work_started",
                {"work_item_id": attempt.work_item_id, "attempt_id": attempt_id},
                actor=f"worker:{attempt.worker_id}",
            )
            return self._append_locked(connection, run_row, dict(run_row.snapshot_json), [event], project=False)

    def renew_lease(self, attempt_id: str, fencing_token: int) -> str:
        now = _now()
        lease_until = now + timedelta(seconds=self.LEASE_SECONDS)
        with self.engine.begin() as connection:
            attempt, item = self._lock_attempt(connection, attempt_id, fencing_token)
            if attempt.status not in {
                WorkItemStatus.LEASED.value,
                WorkItemStatus.RUNNING.value,
                WorkItemStatus.VERIFYING.value,
            }:
                raise StaleLeaseError(f"attempt {attempt_id} is terminal")
            if item.current_attempt_id != attempt_id:
                raise StaleLeaseError(f"attempt {attempt_id} was superseded")
            connection.execute(
                update(attempts).where(attempts.c.id == attempt_id).values(
                    lease_expires_at=lease_until
                )
            )
            connection.execute(
                update(work_items)
                .where(
                    work_items.c.run_id == attempt.run_id,
                    work_items.c.item_id == attempt.work_item_id,
                )
                .values(lease_expires_at=lease_until, updated_at=now)
            )
            if (item.item_json or {}).get("kind") == WorkItemKind.INTEGRATE.value:
                project_id = connection.execute(
                    select(runs.c.project_id).where(runs.c.id == attempt.run_id)
                ).scalar_one()
                connection.execute(
                    update(integration_locks)
                    .where(
                        integration_locks.c.project_id == project_id,
                        integration_locks.c.attempt_id == attempt_id,
                    )
                    .values(lease_expires_at=lease_until, updated_at=now)
                )
            self._upsert_worker(
                connection,
                attempt.worker_id,
                set(),
                now,
                preserve_capabilities=True,
            )
        return lease_until.isoformat()

    def commit_checkpoint(
        self,
        attempt_id: str,
        fencing_token: int,
        checkpoint: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        with self.engine.begin() as connection:
            attempt, _item = self._lock_attempt(connection, attempt_id, fencing_token)
            if attempt.status not in {
                WorkItemStatus.LEASED.value,
                WorkItemStatus.RUNNING.value,
                WorkItemStatus.VERIFYING.value,
            }:
                raise StaleLeaseError(f"attempt {attempt_id} cannot checkpoint")
            last = connection.execute(
                select(checkpoints.c.sequence, checkpoints.c.checkpoint_json)
                .where(checkpoints.c.attempt_id == attempt_id)
                .order_by(checkpoints.c.sequence.desc())
                .limit(1)
            ).first()
            sequence = int(checkpoint.get("sequence", (last.sequence + 1) if last else 1))
            if last and sequence <= int(last.sequence):
                if sequence == int(last.sequence) and dict(last.checkpoint_json) == dict(checkpoint):
                    return self._snapshot_locked(connection, attempt.run_id)
                raise DomainError("checkpoint sequence must increase monotonically")
            payload = dict(checkpoint)
            payload["sequence"] = sequence
            connection.execute(
                insert(checkpoints).values(
                    id=str(uuid.uuid4()),
                    attempt_id=attempt_id,
                    sequence=sequence,
                    checkpoint_json=payload,
                    created_at=now,
                )
            )
            run_row = self._lock_run(connection, attempt.run_id)
            event = WorkflowEvent.create(
                attempt.run_id,
                "checkpoint_committed",
                {
                    "work_item_id": attempt.work_item_id,
                    "attempt_id": attempt_id,
                    "sequence": sequence,
                    "checkpoint": payload,
                },
                actor=f"worker:{attempt.worker_id}",
            )
            return self._append_locked(connection, run_row, dict(run_row.snapshot_json), [event], project=False)

    def complete_attempt(
        self,
        attempt_id: str,
        fencing_token: int,
        outcome: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        with self.engine.begin() as connection:
            attempt, item = self._lock_attempt(connection, attempt_id, fencing_token)
            normalized_outcome = dict(outcome)
            if item.status == WorkItemStatus.CANCELLED.value:
                connection.execute(
                    update(attempts)
                    .where(attempts.c.id == attempt_id)
                    .values(
                        status=WorkItemStatus.CANCELLED.value,
                        completed_at=now,
                        outcome_json={**normalized_outcome, "cancelled": True},
                    )
                )
                connection.execute(
                    update(work_items)
                    .where(
                        work_items.c.run_id == attempt.run_id,
                        work_items.c.item_id == attempt.work_item_id,
                    )
                    .values(
                        lease_owner=None,
                        lease_expires_at=None,
                        current_attempt_id=None,
                        updated_at=now,
                    )
                )
                self._release_integration_lock(connection, attempt, item, now)
                return self._snapshot_locked(connection, attempt.run_id)
            succeeded = bool(
                normalized_outcome.get("succeeded")
                or normalized_outcome.get("status") in {"succeeded", "completed", "passed"}
            )
            target_attempt = "succeeded" if succeeded else "failed"
            retry_delay = 0.0
            if attempt.status in {"succeeded", "failed"}:
                if attempt.status == target_attempt and dict(attempt.outcome_json or {}) == normalized_outcome:
                    return self._snapshot_locked(connection, attempt.run_id)
                raise StaleLeaseError(f"attempt {attempt_id} already completed")
            if item.current_attempt_id != attempt_id:
                raise StaleLeaseError(f"attempt {attempt_id} was superseded")
            connection.execute(
                update(attempts).where(attempts.c.id == attempt_id).values(
                    status=target_attempt,
                    completed_at=now,
                    outcome_json=normalized_outcome,
                )
            )
            if succeeded:
                next_status = WorkItemStatus.SUCCEEDED.value
                event_type = "work_succeeded"
                payload = {
                    "work_item_id": attempt.work_item_id,
                    "attempt_id": attempt_id,
                    "outcome": normalized_outcome,
                }
            else:
                retryable = bool(normalized_outcome.get("retryable", True))
                next_status = (
                    WorkItemStatus.READY.value
                    if retryable and int(item.attempt_count) < int(item.max_attempts)
                    else WorkItemStatus.FAILED.value
                )
                retry_delay = (
                    self.retry_backoff_base_seconds
                    * (2 ** max(0, int(item.attempt_count) - 1))
                    if next_status == WorkItemStatus.READY.value
                    else 0
                )
                event_type = "work_failed"
                payload = {
                    "work_item_id": attempt.work_item_id,
                    "attempt_id": attempt_id,
                    "next_status": next_status,
                    "outcome": normalized_outcome,
                    "retry_after_seconds": retry_delay,
                    "dead_lettered": next_status == WorkItemStatus.FAILED.value,
                }
            connection.execute(
                update(work_items)
                .where(
                    work_items.c.run_id == attempt.run_id,
                    work_items.c.item_id == attempt.work_item_id,
                )
                .values(
                    status=next_status,
                    lease_owner=None,
                    lease_expires_at=None,
                    current_attempt_id=None,
                    not_before=(now + timedelta(seconds=retry_delay)) if retry_delay else None,
                    updated_at=now,
                )
            )
            self._release_integration_lock(connection, attempt, item, now)
            run_row = self._lock_run(connection, attempt.run_id)
            event = WorkflowEvent.create(
                attempt.run_id,
                event_type,
                payload,
                actor=f"worker:{attempt.worker_id}",
            )
            return self._append_locked(connection, run_row, dict(run_row.snapshot_json), [event], project=False)

    def release_expired_leases(self, limit: int = 100) -> int:
        now = _now()
        released = 0
        with self.engine.begin() as connection:
            statement = (
                select(work_items)
                .where(
                    work_items.c.status.in_(
                        [
                            WorkItemStatus.LEASED.value,
                            WorkItemStatus.RUNNING.value,
                            WorkItemStatus.VERIFYING.value,
                        ]
                    ),
                    work_items.c.lease_expires_at < now,
                )
                .order_by(work_items.c.lease_expires_at)
                .limit(limit)
                .with_for_update(skip_locked=self.engine.dialect.name == "postgresql")
            )
            for item in connection.execute(statement).mappings():
                next_status = (
                    WorkItemStatus.READY.value
                    if int(item["attempt_count"]) < int(item["max_attempts"])
                    else WorkItemStatus.FAILED.value
                )
                retry_delay = (
                    self.retry_backoff_base_seconds
                    * (2 ** max(0, int(item["attempt_count"]) - 1))
                    if next_status == WorkItemStatus.READY.value
                    else 0
                )
                if item["current_attempt_id"]:
                    connection.execute(
                        update(attempts)
                        .where(attempts.c.id == item["current_attempt_id"])
                        .values(status="expired", completed_at=now)
                    )
                connection.execute(
                    update(work_items)
                    .where(
                        work_items.c.run_id == item["run_id"],
                        work_items.c.item_id == item["item_id"],
                        work_items.c.fencing_token == item["fencing_token"],
                    )
                    .values(
                        status=next_status,
                        lease_owner=None,
                        lease_expires_at=None,
                        current_attempt_id=None,
                        not_before=(now + timedelta(seconds=retry_delay)) if retry_delay else None,
                        updated_at=now,
                    )
                )
                if (item["item_json"] or {}).get("kind") == WorkItemKind.INTEGRATE.value:
                    project_id = connection.execute(
                        select(runs.c.project_id).where(runs.c.id == item["run_id"])
                    ).scalar_one()
                    connection.execute(
                        update(integration_locks)
                        .where(
                            integration_locks.c.project_id == project_id,
                            integration_locks.c.attempt_id == item["current_attempt_id"],
                        )
                        .values(attempt_id=None, lease_expires_at=None, updated_at=now)
                    )
                run_row = self._lock_run(connection, item["run_id"])
                event = WorkflowEvent.create(
                    item["run_id"],
                    "work_failed",
                    {
                        "work_item_id": item["item_id"],
                        "attempt_id": item["current_attempt_id"],
                        "next_status": next_status,
                        "outcome": {"failure_kind": "lease_expired", "retryable": True},
                        "retry_after_seconds": retry_delay,
                        "dead_lettered": next_status == WorkItemStatus.FAILED.value,
                    },
                    actor="lease-reaper",
                )
                self._append_locked(connection, run_row, dict(run_row.snapshot_json), [event], project=False)
                released += 1
        return released

    def reserve_effect(
        self,
        effect_id: str,
        *,
        run_id: str,
        work_item_id: str,
        attempt_id: str,
        sequence: int,
    ) -> dict[str, Any]:
        now = _now()
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(effect_ledger).where(effect_ledger.c.effect_id == effect_id)
            ).mappings().first()
            if existing:
                return {
                    "status": str(existing["status"]),
                    "result": (
                        dict(existing["result_json"])
                        if existing["result_json"] is not None
                        else None
                    ),
                }
            connection.execute(
                insert(effect_ledger).values(
                    effect_id=effect_id,
                    run_id=run_id,
                    work_item_id=work_item_id,
                    attempt_id=attempt_id,
                    sequence=sequence,
                    status="started",
                    created_at=now,
                    updated_at=now,
                )
            )
        return {"status": "reserved", "result": None}

    def register_artifact(
        self,
        run_id: str,
        artifact: Mapping[str, Any],
        *,
        work_item_id: str | None = None,
        actor: str = "worker",
    ) -> dict[str, Any]:
        snapshot = self.get_snapshot(run_id)
        payload = {**dict(artifact), "work_item_id": work_item_id}
        if not payload.get("id") or not payload.get("uri") or not payload.get("sha256"):
            raise DomainError("artifact id, uri, and sha256 are required")
        return self.append(
            run_id,
            snapshot["version"],
            [WorkflowEvent.create(run_id, "artifact_registered", payload, actor=actor)],
        )

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(artifacts).where(artifacts.c.id == artifact_id)
            ).mappings().first()
        if row is None:
            raise FileNotFoundError(artifact_id)
        return dict(row)

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(artifacts)
                    .where(artifacts.c.run_id == run_id)
                    .order_by(artifacts.c.created_at)
                ).mappings()
            ]

    def list_approvals(
        self,
        project_id: str,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        statement = (
            select(approvals, runs.c.project_id)
            .join(runs, runs.c.id == approvals.c.run_id)
            .where(runs.c.project_id == project_id)
        )
        if status is not None:
            statement = statement.where(approvals.c.status == status)
        statement = statement.order_by(approvals.c.created_at.desc()).limit(
            max(1, min(limit, 1000))
        )
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def complete_effect(self, effect_id: str, result: Mapping[str, Any]) -> None:
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(effect_ledger.c.status, effect_ledger.c.result_json).where(
                    effect_ledger.c.effect_id == effect_id
                )
            ).first()
            if existing is None:
                raise FileNotFoundError(effect_id)
            if existing.status == "completed":
                if dict(existing.result_json or {}) != dict(result):
                    raise DomainError("completed effect result is immutable")
                return
            changed = connection.execute(
                update(effect_ledger)
                .where(effect_ledger.c.effect_id == effect_id)
                .values(status="completed", result_json=dict(result), updated_at=_now())
            )
            if changed.rowcount != 1:
                raise DomainError(f"effect {effect_id} could not be completed")

    def list_outbox(self, after_created_at: datetime | None = None, limit: int = 200) -> list[dict[str, Any]]:
        statement = select(outbox).where(outbox.c.published_at.is_(None))
        if after_created_at:
            statement = statement.where(outbox.c.created_at > after_created_at)
        statement = statement.order_by(outbox.c.created_at).limit(limit)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def mark_outbox_published(self, outbox_ids: Sequence[str]) -> None:
        if not outbox_ids:
            return
        with self.engine.begin() as connection:
            connection.execute(
                update(outbox)
                .where(outbox.c.id.in_(list(outbox_ids)))
                .values(published_at=_now())
            )

    def list_workers(self) -> list[dict[str, Any]]:
        online_after = _now() - timedelta(seconds=self.LEASE_SECONDS * 2)
        with self.engine.connect() as connection:
            return [
                {
                    **dict(row),
                    "online": _as_utc(row["last_seen_at"]) >= online_after,
                }
                for row in connection.execute(select(workers)).mappings()
            ]

    def queue_stats(self, project_id: str | None = None) -> dict[str, Any]:
        statement = select(work_items.c.status, func.count()).join(
            runs, runs.c.id == work_items.c.run_id
        )
        if project_id is not None:
            statement = statement.where(runs.c.project_id == project_id)
        statement = statement.group_by(work_items.c.status)
        with self.engine.connect() as connection:
            counts = {str(status): int(count) for status, count in connection.execute(statement)}
            expired_statement = (
                select(func.count())
                .select_from(work_items.join(runs, runs.c.id == work_items.c.run_id))
                .where(
                    work_items.c.status.in_(
                        [
                            WorkItemStatus.LEASED.value,
                            WorkItemStatus.RUNNING.value,
                            WorkItemStatus.VERIFYING.value,
                        ]
                    ),
                    work_items.c.lease_expires_at < _now(),
                )
            )
            if project_id is not None:
                expired_statement = expired_statement.where(runs.c.project_id == project_id)
            expired = int(connection.execute(expired_statement).scalar_one())
        return {
            "by_status": counts,
            "ready": counts.get(WorkItemStatus.READY.value, 0),
            "active": sum(
                counts.get(status, 0)
                for status in (
                    WorkItemStatus.LEASED.value,
                    WorkItemStatus.RUNNING.value,
                    WorkItemStatus.VERIFYING.value,
                )
            ),
            "expired_leases": expired,
            "dead_lettered": counts.get(WorkItemStatus.FAILED.value, 0),
        }

    def _append_locked(
        self,
        connection: Connection,
        run_row,
        aggregate: dict[str, Any],
        events_to_append: Sequence[WorkflowEvent],
        *,
        project: bool = True,
    ) -> dict[str, Any]:
        version = int(run_row.version)
        for event in events_to_append:
            if event.run_id != run_row.id:
                raise DomainError("cannot append event to a different run")
            version += 1
            materialized = replace(event, version=version)
            aggregate = apply_event(aggregate, materialized)
            self._record_event(connection, materialized)
            if project:
                self._project_event(connection, materialized, aggregate)
        changed = connection.execute(
            update(runs)
            .where(runs.c.id == run_row.id, runs.c.version == run_row.version)
            .values(
                status=aggregate["status"],
                version=aggregate["version"],
                current_plan_revision=aggregate.get("current_plan_revision"),
                snapshot_json=aggregate,
                updated_at=_parse_datetime(aggregate["updated_at"]),
            )
        )
        if changed.rowcount != 1:
            raise ConcurrencyError(f"run {run_row.id} changed during append")
        return aggregate

    def _release_integration_lock(self, connection, attempt, item, now) -> None:
        if (item.item_json or {}).get("kind") != WorkItemKind.INTEGRATE.value:
            return
        project_id = connection.execute(
            select(runs.c.project_id).where(runs.c.id == attempt.run_id)
        ).scalar_one()
        connection.execute(
            update(integration_locks)
            .where(
                integration_locks.c.project_id == project_id,
                integration_locks.c.attempt_id == attempt.id,
            )
            .values(attempt_id=None, lease_expires_at=None, updated_at=now)
        )

    def _record_event(self, connection: Connection, event: WorkflowEvent) -> None:
        created_at = _parse_datetime(event.created_at)
        connection.execute(
            insert(workflow_events).values(
                event_id=event.event_id,
                run_id=event.run_id,
                version=event.version,
                event_type=event.event_type,
                payload_json=event.payload,
                actor=event.actor,
                created_at=created_at,
            )
        )
        connection.execute(
            insert(outbox).values(
                id=str(uuid.uuid4()),
                event_id=event.event_id,
                run_id=event.run_id,
                event_type=event.event_type,
                payload_json={
                    "event_id": event.event_id,
                    "version": event.version,
                    "actor": event.actor,
                    "created_at": event.created_at,
                    "payload": event.payload,
                },
                created_at=created_at,
            )
        )

    def _project_event(
        self,
        connection: Connection,
        event: WorkflowEvent,
        aggregate: Mapping[str, Any],
    ) -> None:
        now = _parse_datetime(event.created_at)
        payload = event.payload
        if event.event_type == "plan_committed":
            plan = dict(payload["plan"])
            connection.execute(
                update(approvals)
                .where(
                    approvals.c.run_id == event.run_id,
                    approvals.c.status.in_(["pending", "approved"]),
                    approvals.c.plan_revision != plan["revision"],
                )
                .values(status="invalidated", decided_at=now)
            )
            connection.execute(
                insert(plan_revisions).values(
                    run_id=event.run_id,
                    revision=plan["revision"],
                    plan_json=plan,
                    reason=payload.get("reason"),
                    created_at=now,
                )
            )
            ids: list[str] = []
            for item in plan["work_items"]:
                item_id = item["id"]
                ids.append(item_id)
                existing = connection.execute(
                    select(work_items.c.status).where(
                        work_items.c.run_id == event.run_id,
                        work_items.c.item_id == item_id,
                    )
                ).scalar_one_or_none()
                values = {
                    "plan_revision": plan["revision"],
                    "item_json": item,
                    "required_capabilities_json": item.get("required_capabilities", []),
                    "max_attempts": int((item.get("budget") or {}).get("max_attempts", 3)),
                    "updated_at": now,
                }
                if existing is None:
                    connection.execute(
                        insert(work_items).values(
                            run_id=event.run_id,
                            item_id=item_id,
                            status=WorkItemStatus.PENDING.value,
                            priority=0,
                            not_before=None,
                            attempt_count=0,
                            fencing_token=0,
                            created_at=now,
                            **values,
                        )
                    )
                else:
                    connection.execute(
                        update(work_items)
                        .where(
                            work_items.c.run_id == event.run_id,
                            work_items.c.item_id == item_id,
                        )
                        .values(**values)
                    )
            stale = update(work_items).where(
                work_items.c.run_id == event.run_id,
                work_items.c.status.notin_([WorkItemStatus.SUCCEEDED.value]),
            )
            if ids:
                stale = stale.where(work_items.c.item_id.notin_(ids))
            connection.execute(stale.values(status=WorkItemStatus.CANCELLED.value, updated_at=now))
        elif event.event_type in {
            "work_ready",
            "work_blocked",
            "work_cancelled",
        }:
            target = {
                "work_ready": WorkItemStatus.READY.value,
                "work_blocked": WorkItemStatus.BLOCKED.value,
                "work_cancelled": WorkItemStatus.CANCELLED.value,
            }[event.event_type]
            item_filter = (
                work_items.c.run_id == event.run_id,
                work_items.c.item_id == payload["work_item_id"],
            )
            values: dict[str, Any] = {"status": target, "updated_at": now}
            if event.event_type == "work_cancelled":
                current_attempt_id = connection.execute(
                    select(work_items.c.current_attempt_id).where(*item_filter)
                ).scalar_one_or_none()
                if current_attempt_id:
                    connection.execute(
                        update(attempts)
                        .where(
                            attempts.c.id == current_attempt_id,
                            attempts.c.status.in_([
                                WorkItemStatus.LEASED.value,
                                WorkItemStatus.RUNNING.value,
                                WorkItemStatus.VERIFYING.value,
                            ]),
                        )
                        .values(
                            status=WorkItemStatus.CANCELLED.value,
                            completed_at=now,
                            outcome_json={
                                "cancelled": True,
                                "reason": payload.get("reason", "work cancelled"),
                            },
                        )
                    )
                    connection.execute(
                        update(integration_locks)
                        .where(integration_locks.c.attempt_id == current_attempt_id)
                        .values(
                            attempt_id=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
                values.update(
                    current_attempt_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    not_before=None,
                )
            connection.execute(update(work_items).where(*item_filter).values(**values))
        elif event.event_type in {
            "work_leased",
            "work_started",
            "work_succeeded",
            "work_failed",
        }:
            item_id = payload["work_item_id"]
            if event.event_type == "work_leased":
                target = WorkItemStatus.LEASED.value
            elif event.event_type == "work_started":
                item_payload = connection.execute(
                    select(work_items.c.item_json).where(
                        work_items.c.run_id == event.run_id,
                        work_items.c.item_id == item_id,
                    )
                ).scalar_one()
                target = (
                    WorkItemStatus.VERIFYING.value
                    if item_payload.get("kind") == "verify"
                    else WorkItemStatus.RUNNING.value
                )
            elif event.event_type == "work_succeeded":
                target = WorkItemStatus.SUCCEEDED.value
            else:
                target = payload.get("next_status", WorkItemStatus.FAILED.value)
            values: dict[str, Any] = {"status": target, "updated_at": now}
            if event.event_type == "work_leased":
                values["current_attempt_id"] = payload.get("attempt_id")
            if event.event_type in {"work_succeeded", "work_failed"}:
                values.update(
                    current_attempt_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            connection.execute(
                update(work_items)
                .where(
                    work_items.c.run_id == event.run_id,
                    work_items.c.item_id == item_id,
                )
                .values(**values)
            )
            if event.event_type == "work_failed":
                connection.execute(
                    insert(evidence).values(
                        id=event.event_id,
                        run_id=event.run_id,
                        work_item_id=item_id,
                        kind="execution_failure",
                        payload_json={
                            "attempt_id": payload.get("attempt_id"),
                            "outcome": payload.get("outcome") or {},
                            "next_status": target,
                        },
                        created_at=now,
                    )
                )
        elif event.event_type == "evidence_recorded":
            connection.execute(
                insert(evidence).values(
                    id=str(payload.get("evidence_id") or event.event_id),
                    run_id=event.run_id,
                    work_item_id=payload.get("work_item_id"),
                    kind=str(payload.get("kind", "diagnostic")),
                    payload_json=dict(payload),
                    created_at=now,
                )
            )
        elif event.event_type == "artifact_registered":
            existing = connection.execute(
                select(artifacts.c.id).where(artifacts.c.id == payload["id"])
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    insert(artifacts).values(
                        id=payload["id"],
                        run_id=event.run_id,
                        work_item_id=payload.get("work_item_id"),
                        uri=payload["uri"],
                        sha256=payload["sha256"],
                        size=int(payload.get("size", 0)),
                        media_type=payload.get("media_type"),
                        sensitivity=payload.get("sensitivity", "internal"),
                        name=payload.get("name"),
                        metadata_json={
                            key: value
                            for key, value in payload.items()
                            if key
                            not in {
                                "id",
                                "run_id",
                                "work_item_id",
                                "uri",
                                "sha256",
                                "size",
                                "media_type",
                                "sensitivity",
                                "name",
                            }
                        },
                        created_at=now,
                    )
                )
        elif event.event_type == "approval_requested":
            connection.execute(
                insert(approvals).values(
                    id=payload["approval_id"],
                    run_id=event.run_id,
                    plan_revision=payload["plan_revision"],
                    effect_digest=payload["effect_digest"],
                    status="pending",
                    requested_by=event.actor,
                    created_at=now,
                )
            )
        elif event.event_type == "approval_decided":
            connection.execute(
                update(approvals)
                .where(approvals.c.id == payload["approval_id"])
                .values(
                    status="approved" if payload.get("approved") else "rejected",
                    decided_by=payload.get("decided_by"),
                    decision_reason=payload.get("reason"),
                    decided_at=now,
                )
            )

    def _lock_run(self, connection: Connection, run_id: str):
        row = connection.execute(
            select(runs)
            .where(runs.c.id == run_id)
            .with_for_update()
        ).first()
        if row is None:
            raise FileNotFoundError(run_id)
        return row

    def _lock_attempt(self, connection: Connection, attempt_id: str, fencing_token: int):
        attempt = connection.execute(
            select(attempts).where(attempts.c.id == attempt_id).with_for_update()
        ).first()
        if attempt is None:
            raise FileNotFoundError(attempt_id)
        item = connection.execute(
            select(work_items)
            .where(
                work_items.c.run_id == attempt.run_id,
                work_items.c.item_id == attempt.work_item_id,
            )
            .with_for_update()
        ).first()
        if item is None or int(attempt.fencing_token) != fencing_token or int(item.fencing_token) != fencing_token:
            raise StaleLeaseError(f"stale fencing token for attempt {attempt_id}")
        return attempt, item

    def _snapshot_locked(self, connection: Connection, run_id: str) -> dict[str, Any]:
        return dict(
            connection.execute(
                select(runs.c.snapshot_json).where(runs.c.id == run_id)
            ).scalar_one()
        )

    def _latest_checkpoint_for_item(
        self, connection: Connection, run_id: str, item_id: str
    ) -> dict[str, Any] | None:
        value = connection.execute(
            select(checkpoints.c.checkpoint_json)
            .join(attempts, attempts.c.id == checkpoints.c.attempt_id)
            .where(attempts.c.run_id == run_id, attempts.c.work_item_id == item_id)
            .order_by(checkpoints.c.created_at.desc(), checkpoints.c.sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        return dict(value) if value is not None else None

    def _upsert_worker(
        self,
        connection: Connection,
        worker_id: str,
        capabilities: set[str],
        now: datetime,
        *,
        preserve_capabilities: bool = False,
    ) -> None:
        current = connection.execute(
            select(workers.c.id).where(workers.c.id == worker_id)
        ).scalar_one_or_none()
        if current is None:
            connection.execute(
                insert(workers).values(
                    id=worker_id,
                    capabilities_json=sorted(capabilities),
                    status="online",
                    last_seen_at=now,
                )
            )
        else:
            values: dict[str, Any] = {"status": "online", "last_seen_at": now}
            if not preserve_capabilities:
                values["capabilities_json"] = sorted(capabilities)
            connection.execute(
                update(workers).where(workers.c.id == worker_id).values(**values)
            )

    @staticmethod
    def _event_from_row(row: Mapping[str, Any]) -> WorkflowEvent:
        return WorkflowEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            version=row["version"],
            event_type=row["event_type"],
            payload=dict(row["payload_json"]),
            actor=row["actor"],
            created_at=_as_utc(row["created_at"]).isoformat(),
        )


def repository_from_url(database_url: str) -> SqlWorkflowRepository:
    return SqlWorkflowRepository(database_url)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _insert_do_nothing(
    connection: Connection,
    table: Table,
    values: Mapping[str, Any],
    index_elements: Sequence[str],
):
    if connection.dialect.name == "postgresql":
        return postgresql_insert(table).values(**values).on_conflict_do_nothing(
            index_elements=list(index_elements)
        )
    if connection.dialect.name == "sqlite":
        return sqlite_insert(table).values(**values).on_conflict_do_nothing(
            index_elements=list(index_elements)
        )
    return insert(table).values(**values)


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    return _as_utc(datetime.fromisoformat(value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
