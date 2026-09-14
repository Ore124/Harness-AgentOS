"""CLI entry points for the durable controller and independent workers."""
from __future__ import annotations

import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import config
from orchestrator.controller import DurableController, EvidenceRecoveryPlanner
from orchestrator.legacy_import import import_legacy_state
from orchestrator.runtime import (
    DockerProfileAgentAdapter,
    DurableWorker,
    GitWorktreeAgentAdapter,
    ProfileAgentAdapter,
    standard_profile_plan,
)
from orchestrator.workflow_repository import SqlWorkflowRepository


def open_repository() -> SqlWorkflowRepository:
    return SqlWorkflowRepository(
        config.DATABASE_URL,
        retry_backoff_base_seconds=config.RETRY_BACKOFF_SECONDS,
    )


def run_task(goal: str, profile: str) -> dict[str, Any]:
    repository = open_repository()
    run_id = str(uuid.uuid4())
    workspace = str((Path(config.WORKSPACE).resolve() / "durable" / run_id).resolve())
    controller = DurableController(repository, planner=EvidenceRecoveryPlanner())
    state = controller.create_run(
        "default",
        goal,
        run_id=run_id,
        policy={
            "profile": profile,
            "workspace": workspace,
            "max_total_seconds": 3600,
            "execution_capability": config.DURABLE_EXECUTION_CAPABILITY,
            **_git_settings(),
            "allow_terminal": True,
        },
    )
    controller.commit_plan(
        run_id,
        standard_profile_plan(
            goal,
            profile,
            execution_capability=config.DURABLE_EXECUTION_CAPABILITY,
            git_settings=_git_settings() or None,
        ),
        expected_version=state["version"],
    )
    controller.advance(run_id)
    worker = DurableWorker(
        f"inline-{socket.gethostname()}-{uuid.uuid4().hex[:8]}",
        repository,
        _agent_adapter(),
        capabilities={config.DURABLE_EXECUTION_CAPABILITY},
        controller=controller,
        heartbeat_seconds=config.WORKER_HEARTBEAT_SECONDS,
        artifact_store=_artifact_store(),
        target_run_id=run_id,
    )
    return _run_inline_until_terminal(
        repository,
        worker,
        run_id,
        config.WORKER_POLL_SECONDS,
    )


def _run_inline_until_terminal(
    repository: SqlWorkflowRepository,
    worker: DurableWorker,
    run_id: str,
    poll_interval: float,
) -> dict[str, Any]:
    terminal_or_waiting = {
        "completed",
        "failed",
        "cancelled",
        "paused",
        "waiting_approval",
    }
    while True:
        snapshot = repository.get_snapshot(run_id)
        if snapshot.get("status") in terminal_or_waiting:
            return snapshot
        if not worker.run_once():
            time.sleep(poll_interval)


def worker_main(worker_id: str | None = None) -> None:
    from orchestrator.observability import configure
    from orchestrator.outbox import OpenTelemetryEventSink, OutboxDispatcher

    configure(f"{config.OTEL_SERVICE_NAME}-worker", config.OTEL_EXPORTER_OTLP_ENDPOINT)
    repository = open_repository()
    resolved_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    worker = DurableWorker(
        resolved_id,
        repository,
        _agent_adapter(),
        capabilities=set(config.WORKER_CAPABILITIES),
        heartbeat_seconds=config.WORKER_HEARTBEAT_SECONDS,
        artifact_store=_artifact_store(),
    )
    stop_event = threading.Event()
    dispatcher = OutboxDispatcher(repository, OpenTelemetryEventSink())
    outbox_thread = threading.Thread(
        target=dispatcher.run_forever,
        kwargs={"poll_interval": config.WORKER_POLL_SECONDS, "stop_event": stop_event},
        daemon=True,
        name=f"outbox-{resolved_id}",
    )
    outbox_thread.start()
    try:
        worker.run_forever(
            poll_interval=config.WORKER_POLL_SECONDS,
            stop_event=stop_event,
        )
    finally:
        stop_event.set()
        outbox_thread.join(timeout=5)


def migrate_state_main(state_path: str, project_id: str = "legacy-imports") -> dict[str, Any]:
    repository = open_repository()
    return import_legacy_state(state_path, repository, project_id=project_id)


def set_membership_main(project_id: str, subject: str, role: str) -> None:
    repository = open_repository()
    repository.set_membership(project_id, subject, role)


def _agent_adapter():
    if config.WORKER_EXECUTOR == "docker":
        from orchestrator.sandbox import DockerExecutor, SandboxPolicy

        policy = SandboxPolicy(
            image=config.AGENT_SANDBOX_IMAGE,
            allowed_images=(config.AGENT_SANDBOX_IMAGE,),
            allow_network_hosts=config.AGENT_NETWORK_HOSTS,
            egress_network=config.AGENT_EGRESS_NETWORK,
            egress_proxy_url=config.AGENT_EGRESS_PROXY,
            allowed_workspace_roots=tuple(
                Path(value).expanduser().resolve()
                for value in (config.WORKSPACE, config.GIT_WORKTREE_ROOT)
            ),
        )
        adapter = DockerProfileAgentAdapter(DockerExecutor(), policy)
    else:
        adapter = ProfileAgentAdapter()
    if config.GIT_REPOSITORY:
        from orchestrator.git_workspace import GitWorktreeManager

        adapter = GitWorktreeAgentAdapter(
            adapter,
            GitWorktreeManager(config.GIT_REPOSITORY, config.GIT_WORKTREE_ROOT),
        )
    return adapter


def _git_settings() -> dict[str, str]:
    if not config.GIT_REPOSITORY:
        return {}
    return {
        "repository": str(Path(config.GIT_REPOSITORY).expanduser().resolve()),
        "base_revision": config.GIT_BASE_REVISION,
        "target_branch": config.GIT_TARGET_BRANCH,
        "worktree_root": str(Path(config.GIT_WORKTREE_ROOT).expanduser().resolve()),
    }


def _artifact_store():
    from orchestrator.artifacts import LocalArtifactStore, S3ArtifactStore

    if config.ARTIFACT_STORE == "s3":
        if not config.S3_BUCKET:
            raise RuntimeError("HARNESS_S3_BUCKET is required for S3 artifacts")
        return S3ArtifactStore(
            config.S3_BUCKET,
            prefix=config.S3_PREFIX,
            endpoint_url=config.S3_ENDPOINT_URL,
            public_endpoint_url=config.S3_PUBLIC_ENDPOINT_URL,
        )
    return LocalArtifactStore(config.ARTIFACT_ROOT)
