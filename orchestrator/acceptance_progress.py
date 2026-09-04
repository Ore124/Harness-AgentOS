"""Acceptance-evidence driven progress decisions.

This module is deliberately independent from the scheduler.  It implements a
small, JSON-serializable state machine that decides what a long-running task
should do from durable acceptance evidence instead of round counts.

The important invariant is that a successful verification belongs to one
artifact revision.  Changing an artifact advances the revision and therefore
invalidates every verification from an older revision.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping


DecisionKind = Literal["complete", "verify", "repair", "continue", "blocked"]
VerificationStatus = Literal["missing", "running", "passed", "failed"]

STATE_VERSION = 1
VALID_VERIFICATION_STATUSES = {"missing", "running", "passed", "failed"}


@dataclass(frozen=True)
class AcceptanceObservation:
    """One durable progress observation supplied by the orchestration layer.

    ``observation_id`` must be stable across retries of the same scheduler
    transition.  Concrete verification results require a stable token (for
    example the trace event id or trace offset) so an old successful result
    cannot accidentally validate newly changed artifacts.
    """

    observation_id: str
    artifacts_changed: bool = False
    verification_status: VerificationStatus = "missing"
    verification_token: str | None = None
    recent_failure: str | None = None
    remaining_budget_seconds: float | None = None
    progress_made: bool | None = None

    def __post_init__(self) -> None:
        if not self.observation_id or not self.observation_id.strip():
            raise ValueError("observation_id must be a non-empty stable identifier")
        if self.verification_status not in VALID_VERIFICATION_STATUSES:
            raise ValueError(f"unknown verification_status: {self.verification_status!r}")
        if self.verification_status in {"passed", "failed"} and not self.verification_token:
            raise ValueError("passed/failed verification requires verification_token")
        if self.remaining_budget_seconds is not None and self.remaining_budget_seconds < 0:
            raise ValueError("remaining_budget_seconds cannot be negative")


@dataclass(frozen=True)
class AcceptanceDecision:
    """A progress decision plus enough context for scheduler integration."""

    action: DecisionKind
    reason: str
    artifact_revision: int
    verified_revision: int | None
    no_progress_count: int
    recovery_recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AcceptanceDecision":
        return cls(
            action=value["action"],
            reason=str(value.get("reason") or ""),
            artifact_revision=int(value.get("artifact_revision", 0)),
            verified_revision=(
                int(value["verified_revision"])
                if value.get("verified_revision") is not None
                else None
            ),
            no_progress_count=int(value.get("no_progress_count", 0)),
            recovery_recommended=bool(value.get("recovery_recommended", False)),
        )


def create_acceptance_progress_state() -> dict[str, Any]:
    """Return a fresh state suitable for embedding in ``harness_state.json``."""
    return {
        "version": STATE_VERSION,
        "artifact_revision": 0,
        "verified_revision": None,
        "last_verification": None,
        "seen_verification_tokens": [],
        "no_progress_count": 0,
        "failure_count": 0,
        "observation_count": 0,
        "last_observation_id": None,
        "last_observation_digest": None,
        "last_decision": None,
        "history": [],
    }


def build_acceptance_summary(
    progress: Mapping[str, Any] | None,
    analysis: Mapping[str, Any] | None = None,
    decision: Mapping[str, Any] | AcceptanceDecision | None = None,
    *,
    repair_rounds: int = 0,
) -> dict[str, Any]:
    """Build a compact, stable summary for final state and run metrics.

    The controller state remains the source of truth for revisions.  Trace
    analysis supplies verification/staleness counts because it can distinguish
    real verification tool calls from evaluator score observations.
    """
    normalized = _normalize_state(progress)
    verification = ((analysis or {}).get("verification") or {})
    attempts = verification.get("attempts") or []
    distinct_tokens = {
        str(attempt.get("token"))
        for attempt in attempts
        if isinstance(attempt, Mapping) and attempt.get("token")
    }
    verification_attempts = int(
        verification.get("attempt_count_total")
        if verification.get("attempt_count_total") is not None
        else (len(distinct_tokens) if distinct_tokens else len(attempts))
    )
    stale_verifications = int(
        verification.get("stale_count_total")
        if verification.get("stale_count_total") is not None
        else sum(
            1
            for attempt in attempts
            if isinstance(attempt, Mapping) and attempt.get("stale")
        )
    )

    decision_value = (
        decision.to_dict()
        if isinstance(decision, AcceptanceDecision)
        else dict(decision or normalized.get("last_decision") or {})
    )
    artifact_revision = int(normalized["artifact_revision"])
    verified_revision = normalized.get("verified_revision")
    accepted_revision = (
        verified_revision
        if decision_value.get("action") == "complete" and verified_revision == artifact_revision
        else None
    )
    return {
        "artifact_revisions": artifact_revision,
        "verification_attempts": verification_attempts,
        "stale_verifications": stale_verifications,
        "repair_rounds": max(0, int(repair_rounds)),
        "final_accepted_revision": accepted_revision,
        "completion_reason": str(decision_value.get("reason") or "acceptance decision unavailable"),
    }


class AcceptanceProgressController:
    """Pure acceptance-driven policy with serializable, restart-safe state."""

    def __init__(self, *, no_progress_limit: int = 3, history_limit: int = 50):
        if no_progress_limit < 1:
            raise ValueError("no_progress_limit must be at least 1")
        if history_limit < 1:
            raise ValueError("history_limit must be at least 1")
        self.no_progress_limit = no_progress_limit
        self.history_limit = history_limit

    def decide(
        self,
        state: Mapping[str, Any] | None,
        observation: AcceptanceObservation,
    ) -> tuple[dict[str, Any], AcceptanceDecision]:
        """Apply *observation* and return updated state and the next decision.

        The input mapping is never mutated.  Supplying the same observation id
        and payload again is idempotent, which makes replay after interruption
        safe.  Reusing an id for a different payload is rejected.
        """
        current = _normalize_state(state)
        digest = _observation_digest(observation)

        if observation.observation_id == current["last_observation_id"]:
            if digest != current["last_observation_digest"]:
                raise ValueError("observation_id was reused with different content")
            last = current.get("last_decision")
            if not isinstance(last, Mapping):
                raise ValueError("persisted acceptance state has no replayable decision")
            return current, AcceptanceDecision.from_dict(last)

        artifact_revision = int(current["artifact_revision"])
        if observation.artifacts_changed:
            artifact_revision += 1
            current["artifact_revision"] = artifact_revision

        last_verification = current.get("last_verification")
        seen_tokens = list(current.get("seen_verification_tokens") or [])
        same_verification = bool(
            observation.verification_token
            and isinstance(last_verification, Mapping)
            and last_verification.get("token") == observation.verification_token
        )
        finishes_running_verification = bool(
            same_verification
            and last_verification.get("status") == "running"
            and observation.verification_status in {"passed", "failed"}
            and int(last_verification.get("artifact_revision", -1)) == artifact_revision
        )
        fresh_verification = bool(
            observation.verification_token
            and (
                observation.verification_token not in seen_tokens
                or finishes_running_verification
            )
        )
        if fresh_verification:
            last_verification = {
                "token": observation.verification_token,
                "status": observation.verification_status,
                "artifact_revision": artifact_revision,
            }
            current["last_verification"] = last_verification
            if observation.verification_token not in seen_tokens:
                seen_tokens.append(observation.verification_token)
                current["seen_verification_tokens"] = seen_tokens[-500:]
            if observation.verification_status == "passed":
                current["verified_revision"] = artifact_revision
            elif observation.verification_status == "failed":
                if current.get("verified_revision") == artifact_revision:
                    current["verified_revision"] = None
                current["failure_count"] = int(current["failure_count"]) + 1

        objective_progress = (
            observation.progress_made
            if observation.progress_made is not None
            else (
                observation.artifacts_changed
                or observation.verification_status == "running"
                or (
                    fresh_verification
                    and observation.verification_status == "passed"
                )
            )
        )
        if objective_progress:
            current["no_progress_count"] = 0
        else:
            current["no_progress_count"] = int(current["no_progress_count"]) + 1

        current["observation_count"] = int(current["observation_count"]) + 1
        decision = self._choose(current, observation)
        current["last_observation_id"] = observation.observation_id
        current["last_observation_digest"] = digest
        current["last_decision"] = decision.to_dict()

        history = list(current.get("history") or [])
        history.append({
            "observation_id": observation.observation_id,
            "artifacts_changed": observation.artifacts_changed,
            "verification_status": observation.verification_status,
            "verification_token": observation.verification_token,
            "recent_failure": observation.recent_failure,
            "remaining_budget_seconds": observation.remaining_budget_seconds,
            "decision": decision.to_dict(),
        })
        current["history"] = history[-self.history_limit :]
        return current, decision

    def _choose(
        self,
        state: Mapping[str, Any],
        observation: AcceptanceObservation,
    ) -> AcceptanceDecision:
        revision = int(state["artifact_revision"])
        verified_revision = state.get("verified_revision")
        no_progress = int(state["no_progress_count"])
        latest = state.get("last_verification")
        verified_current = verified_revision == revision

        # Acceptance evidence is terminal when no later artifact mutation has
        # invalidated it, even if the outer loop expected additional rounds.
        if verified_current:
            return AcceptanceDecision(
                "complete",
                f"verification passed for current artifact revision {revision}",
                revision,
                verified_revision,
                no_progress,
            )

        if observation.remaining_budget_seconds is not None and observation.remaining_budget_seconds <= 0:
            return AcceptanceDecision(
                "blocked",
                "task budget is exhausted before current artifacts were accepted",
                revision,
                verified_revision,
                no_progress,
                recovery_recommended=False,
            )

        if no_progress >= self.no_progress_limit:
            return AcceptanceDecision(
                "blocked",
                f"no acceptance progress for {no_progress} consecutive observations",
                revision,
                verified_revision,
                no_progress,
                recovery_recommended=True,
            )

        latest_is_current = (
            isinstance(latest, Mapping)
            and int(latest.get("artifact_revision", -1)) == revision
        )
        if latest_is_current and latest.get("status") == "failed":
            return AcceptanceDecision(
                "repair",
                "latest verification failed for current artifacts",
                revision,
                verified_revision,
                no_progress,
            )

        if observation.recent_failure:
            return AcceptanceDecision(
                "repair",
                f"recent execution failure requires repair: {observation.recent_failure}",
                revision,
                verified_revision,
                no_progress,
            )

        if observation.verification_status == "running":
            return AcceptanceDecision(
                "continue",
                "verification is still running",
                revision,
                verified_revision,
                no_progress,
            )

        return AcceptanceDecision(
            "verify",
            f"artifact revision {revision} has no current successful verification",
            revision,
            verified_revision,
            no_progress,
        )


def advance_acceptance_progress(
    state: Mapping[str, Any] | None,
    observation: AcceptanceObservation,
    *,
    no_progress_limit: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Functional wrapper convenient for state-file scheduler integration."""
    updated, decision = AcceptanceProgressController(
        no_progress_limit=no_progress_limit,
    ).decide(state, observation)
    return updated, decision.to_dict()


def _normalize_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = create_acceptance_progress_state()
    if state is not None:
        normalized.update(json.loads(json.dumps(dict(state))))
    if normalized.get("version") != STATE_VERSION:
        raise ValueError(f"unsupported acceptance progress version: {normalized.get('version')}")
    for field in ("artifact_revision", "no_progress_count", "failure_count", "observation_count"):
        value = normalized.get(field)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"acceptance state field {field} must be a non-negative integer")
    verified = normalized.get("verified_revision")
    if verified is not None and (not isinstance(verified, int) or verified < 0):
        raise ValueError("verified_revision must be a non-negative integer or null")
    if not isinstance(normalized.get("history"), list):
        raise ValueError("acceptance state history must be a list")
    if not isinstance(normalized.get("seen_verification_tokens"), list):
        raise ValueError("seen_verification_tokens must be a list")
    # Migrate early controller states that predate the token ledger.
    if state is not None and "seen_verification_tokens" not in state:
        tokens = [
            item.get("verification_token")
            for item in normalized["history"]
            if isinstance(item, Mapping) and item.get("verification_token")
        ]
        last = normalized.get("last_verification")
        if isinstance(last, Mapping) and last.get("token"):
            tokens.append(last["token"])
        normalized["seen_verification_tokens"] = list(dict.fromkeys(tokens))[-500:]
    return normalized


def _observation_digest(observation: AcceptanceObservation) -> str:
    payload = json.dumps(asdict(observation), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
