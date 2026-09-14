"""Versioned durable control-plane API."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

import config
from orchestrator.artifacts import ArtifactRecord, LocalArtifactStore, S3ArtifactStore
from orchestrator.controller import DurableController, EvidenceRecoveryPlanner
from orchestrator.domain import DomainError, RunStatus
from orchestrator.plan_graph import graph_view
from orchestrator.runtime import standard_profile_plan
from orchestrator.security import (
    AuthenticationError,
    Authenticator,
    AuthorizationError,
    authorize_project,
    redact,
)
from orchestrator.workflow_repository import SqlWorkflowRepository


router = APIRouter(prefix="/api/v1", tags=["durable-runs"])
_repository: SqlWorkflowRepository | None = None
_authenticator: Authenticator | None = None
_artifact_store = None


class CreateDurableRunRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1)
    profile: str = "app-builder"
    max_total_seconds: int = Field(default=3600, ge=180, le=86400)
    max_total_tokens: int | None = Field(default=None, ge=1000)
    max_total_cost_usd: float | None = Field(default=None, gt=0)


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: str = ""


def get_repository(request: Request | None = None) -> SqlWorkflowRepository:
    if request is not None and hasattr(request.app.state, "workflow_repository"):
        return request.app.state.workflow_repository
    global _repository
    if _repository is None:
        _repository = SqlWorkflowRepository(config.DATABASE_URL)
    return _repository


def get_authenticator(request: Request | None = None) -> Authenticator:
    if request is not None and hasattr(request.app.state, "authenticator"):
        return request.app.state.authenticator
    global _authenticator
    if _authenticator is None:
        _authenticator = Authenticator(
            config.AUTH_MODE,
            issuer=config.OIDC_ISSUER,
            audience=config.OIDC_AUDIENCE,
            jwks_url=config.OIDC_JWKS_URL,
        )
    return _authenticator


def get_artifact_store():
    global _artifact_store
    if _artifact_store is None:
        if config.ARTIFACT_STORE == "s3":
            if not config.S3_BUCKET:
                raise RuntimeError("HARNESS_S3_BUCKET is required for S3 artifacts")
            _artifact_store = S3ArtifactStore(
                config.S3_BUCKET,
                prefix=config.S3_PREFIX,
                endpoint_url=config.S3_ENDPOINT_URL,
                public_endpoint_url=config.S3_PUBLIC_ENDPOINT_URL,
            )
        else:
            _artifact_store = LocalArtifactStore(config.ARTIFACT_ROOT)
    return _artifact_store


def _context(request: Request):
    try:
        return get_authenticator(request).authenticate(request.headers.get("authorization"))
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _authorize(request: Request, project_id: str, permission: str):
    try:
        return authorize_project(
            _context(request), get_repository(request), project_id, permission
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _snapshot(request: Request, run_id: str, permission: str = "read"):
    repository = get_repository(request)
    try:
        snapshot = repository.get_snapshot(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    _authorize(request, snapshot["project_id"], permission)
    return repository, snapshot


@router.post("/runs")
def create_run(
    payload: CreateDurableRunRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    repository = get_repository(request)
    context = _context(request)
    _authorize(request, payload.project_id, "create_run")
    run_id = str(uuid.uuid4())
    workspace = str((Path(config.WORKSPACE).resolve() / "durable" / run_id).resolve())
    controller = DurableController(repository, planner=EvidenceRecoveryPlanner())
    try:
        state = controller.create_run(
            payload.project_id,
            payload.goal,
            idempotency_key=idempotency_key.strip(),
            run_id=run_id,
            actor=f"user:{context.subject}",
            policy={
                "profile": payload.profile,
                "workspace": workspace,
                "max_total_seconds": payload.max_total_seconds,
                "max_total_tokens": payload.max_total_tokens,
                "max_total_cost_usd": payload.max_total_cost_usd,
                "execution_capability": config.DURABLE_EXECUTION_CAPABILITY,
                **_git_settings(),
                "allow_terminal": config.DURABLE_EXECUTION_CAPABILITY == "local",
            },
        )
        if state.get("plan") is None:
            plan = standard_profile_plan(
                payload.goal,
                payload.profile,
                max_seconds=payload.max_total_seconds,
                max_tokens=payload.max_total_tokens,
                max_cost_usd=payload.max_total_cost_usd,
                execution_capability=config.DURABLE_EXECUTION_CAPABILITY,
                git_settings=_git_settings() or None,
            )
            controller.commit_plan(state["run_id"], plan, expected_version=state["version"])
            controller.advance(state["run_id"])
        state = repository.get_snapshot(state["run_id"])
    except (DomainError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=state)


@router.get("/runs")
def list_runs(request: Request, project_id: str):
    _authorize(request, project_id, "read")
    return {"runs": get_repository(request).list_runs(project_id)}


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    _repository, snapshot = _snapshot(request, run_id)
    return snapshot


@router.get("/runs/{run_id}/graph")
def get_run_graph(run_id: str, request: Request):
    _repository, snapshot = _snapshot(request, run_id)
    if snapshot.get("plan") is None:
        return {"revision": None, "nodes": [], "edges": []}
    from orchestrator.domain import PlanDocumentV1

    return graph_view(
        PlanDocumentV1.from_dict(snapshot["plan"]),
        snapshot.get("work_item_statuses") or {},
    )


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, request: Request, after: int = 0):
    repository, _snapshot_value = _snapshot(request, run_id)

    async def stream():
        cursor = max(0, after)
        while True:
            if await request.is_disconnected():
                break
            events = await asyncio.to_thread(repository.list_events, run_id, cursor, 200)
            for event in events:
                cursor = max(cursor, int(event["version"]))
                yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/runs/{run_id}/pause")
def pause_run(run_id: str, request: Request):
    repository, _snapshot_value = _snapshot(request, run_id, "control_run")
    return DurableController(repository).change_status(
        run_id, RunStatus.PAUSED.value, actor=f"user:{_context(request).subject}"
    )


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: str, request: Request):
    repository, snapshot = _snapshot(request, run_id, "control_run")
    if snapshot["status"] == RunStatus.WAITING_APPROVAL.value:
        raise HTTPException(status_code=409, detail="pending approval must be decided")
    state = DurableController(repository).change_status(
        run_id, RunStatus.RUNNING.value, actor=f"user:{_context(request).subject}"
    )
    DurableController(repository).reconcile(run_id)
    return repository.get_snapshot(state["run_id"])


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request):
    repository, _snapshot_value = _snapshot(request, run_id, "control_run")
    return DurableController(repository).change_status(
        run_id, RunStatus.CANCELLED.value, actor=f"user:{_context(request).subject}"
    )


@router.get("/runs/{run_id}/approvals")
def list_approvals(run_id: str, request: Request):
    _repository, snapshot = _snapshot(request, run_id)
    return {"approvals": list((snapshot.get("approvals") or {}).values())}


@router.get("/approvals")
def list_project_approvals(
    request: Request,
    project_id: str,
    status: str | None = None,
    limit: int = 200,
):
    _authorize(request, project_id, "read")
    return {
        "approvals": get_repository(request).list_approvals(
            project_id,
            status=status,
            limit=limit,
        )
    }


@router.post("/runs/{run_id}/approvals/{approval_id}/decision")
def decide_approval(
    run_id: str,
    approval_id: str,
    payload: ApprovalDecisionRequest,
    request: Request,
):
    repository, _snapshot_value = _snapshot(request, run_id, "approve")
    context = _context(request)
    try:
        state = DurableController(repository).decide_approval(
            run_id,
            approval_id,
            approved=payload.approved,
            decided_by=context.subject,
            reason=payload.reason,
        )
        if payload.approved:
            DurableController(repository).reconcile(run_id)
        return repository.get_snapshot(state["run_id"])
    except DomainError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, request: Request):
    repository = get_repository(request)
    try:
        row = repository.get_artifact(artifact_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    snapshot = repository.get_snapshot(row["run_id"])
    _authorize(request, snapshot["project_id"], "read")
    record = ArtifactRecord(
        id=row["id"],
        uri=row["uri"],
        sha256=row["sha256"],
        size=row["size"],
        media_type=row.get("media_type") or "application/octet-stream",
        sensitivity=row.get("sensitivity") or "internal",
        name=row.get("name"),
    )
    store = get_artifact_store()
    url = store.signed_url(record, expires_seconds=300)
    if url.startswith("file:"):
        with store.open(record):
            pass
        from urllib.parse import unquote, urlparse

        path = unquote(urlparse(url).path)
        if Path(path).drive == "" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return FileResponse(path, media_type=record.media_type, filename=record.name)
    return RedirectResponse(url, status_code=307)


@router.get("/workers")
def list_workers(request: Request, project_id: str):
    _authorize(request, project_id, "read")
    return {"workers": get_repository(request).list_workers()}


@router.get("/projects/{project_id}/audit")
def list_audit_events(
    project_id: str,
    request: Request,
    after: datetime | None = None,
    limit: int = 200,
):
    _authorize(request, project_id, "read")
    events = get_repository(request).list_audit_events(
        project_id,
        after_created_at=after,
        limit=limit,
    )
    return {"events": redact(events)}


@router.get("/projects/{project_id}/queue")
def project_queue(project_id: str, request: Request):
    _authorize(request, project_id, "read")
    return get_repository(request).queue_stats(project_id)


@router.get("/health")
def health(request: Request):
    repository = get_repository(request)
    try:
        with repository.engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {type(exc).__name__}") from exc
    return {
        "status": "ok",
        "runtime": "durable",
        "queue": repository.queue_stats(),
        "workers": repository.list_workers(),
    }


def _git_settings() -> dict[str, str]:
    if not config.GIT_REPOSITORY:
        return {}
    return {
        "repository": str(Path(config.GIT_REPOSITORY).expanduser().resolve()),
        "base_revision": config.GIT_BASE_REVISION,
        "target_branch": config.GIT_TARGET_BRANCH,
        "worktree_root": str(Path(config.GIT_WORKTREE_ROOT).expanduser().resolve()),
    }
