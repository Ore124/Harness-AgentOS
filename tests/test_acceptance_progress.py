import json
import unittest

from orchestrator.acceptance_progress import (
    AcceptanceObservation,
    AcceptanceProgressController,
    advance_acceptance_progress,
    build_acceptance_summary,
    create_acceptance_progress_state,
)


class AcceptanceProgressControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = AcceptanceProgressController(no_progress_limit=3)

    def test_missing_verification_requests_verification(self):
        state, decision = self.controller.decide(
            None,
            AcceptanceObservation("build-1", artifacts_changed=True),
        )

        self.assertEqual(decision.action, "verify")
        self.assertEqual(state["artifact_revision"], 1)
        self.assertEqual(state["no_progress_count"], 0)

    def test_successful_verification_completes_current_revision(self):
        state, _ = self.controller.decide(
            None,
            AcceptanceObservation("build-1", artifacts_changed=True),
        )
        state, decision = self.controller.decide(
            state,
            AcceptanceObservation(
                "verify-1",
                verification_status="passed",
                verification_token="trace:10",
            ),
        )

        self.assertEqual(decision.action, "complete")
        self.assertEqual(decision.verified_revision, 1)

    def test_artifact_change_invalidates_old_successful_verification(self):
        state, _ = self.controller.decide(
            None,
            AcceptanceObservation(
                "verify-1",
                verification_status="passed",
                verification_token="trace:10",
            ),
        )
        state, decision = self.controller.decide(
            state,
            AcceptanceObservation(
                "build-2",
                artifacts_changed=True,
                # Analytics may still expose the old latest attempt.  Its
                # stable token ensures it cannot validate revision 1.
                verification_status="passed",
                verification_token="trace:10",
            ),
        )

        self.assertEqual(decision.action, "verify")
        self.assertEqual(state["artifact_revision"], 1)
        self.assertEqual(state["verified_revision"], 0)

    def test_new_verification_after_change_accepts_new_revision(self):
        state, _ = self.controller.decide(
            None,
            AcceptanceObservation("build-1", artifacts_changed=True),
        )
        state, decision = self.controller.decide(
            state,
            AcceptanceObservation(
                "verify-1",
                verification_status="passed",
                verification_token="trace:20",
            ),
        )

        self.assertEqual(decision.action, "complete")
        self.assertEqual(state["verified_revision"], 1)

    def test_failed_verification_requests_repair(self):
        state, decision = self.controller.decide(
            None,
            AcceptanceObservation(
                "verify-1",
                verification_status="failed",
                verification_token="trace:5",
            ),
        )

        self.assertEqual(decision.action, "repair")
        self.assertEqual(state["failure_count"], 1)

    def test_later_failure_revokes_current_success(self):
        state, first = self.controller.decide(
            None,
            AcceptanceObservation(
                "verify-pass",
                verification_status="passed",
                verification_token="trace:pass",
            ),
        )
        state, second = self.controller.decide(
            state,
            AcceptanceObservation(
                "score-fail",
                verification_status="failed",
                verification_token="score:round-1",
            ),
        )

        self.assertEqual(first.action, "complete")
        self.assertEqual(second.action, "repair")
        self.assertIsNone(state["verified_revision"])

    def test_seen_old_pass_cannot_override_later_failure(self):
        state, _ = self.controller.decide(
            None,
            AcceptanceObservation(
                "verify-pass",
                verification_status="passed",
                verification_token="trace:pass",
            ),
        )
        state, _ = self.controller.decide(
            state,
            AcceptanceObservation(
                "score-fail",
                verification_status="failed",
                verification_token="score:round-1",
            ),
        )
        state, decision = self.controller.decide(
            state,
            AcceptanceObservation(
                "refresh-old-pass",
                verification_status="passed",
                verification_token="trace:pass",
            ),
        )

        self.assertEqual(decision.action, "repair")
        self.assertIsNone(state["verified_revision"])

    def test_repeated_failed_verification_eventually_blocks_for_recovery(self):
        state = None
        for index in range(3):
            state, decision = self.controller.decide(
                state,
                AcceptanceObservation(
                    f"verify-{index}",
                    verification_status="failed",
                    verification_token=f"trace:{index}",
                ),
            )

        self.assertEqual(decision.action, "blocked")
        self.assertTrue(decision.recovery_recommended)
        self.assertEqual(state["failure_count"], 3)

    def test_recent_execution_failure_requests_repair(self):
        _, decision = self.controller.decide(
            None,
            AcceptanceObservation("agent-1", recent_failure="tool timeout"),
        )

        self.assertEqual(decision.action, "repair")
        self.assertIn("tool timeout", decision.reason)

    def test_running_verification_continues(self):
        _, decision = self.controller.decide(
            None,
            AcceptanceObservation("verify-start", verification_status="running"),
        )

        self.assertEqual(decision.action, "continue")

    def test_running_verification_can_finish_with_same_token(self):
        state, first = self.controller.decide(
            None,
            AcceptanceObservation(
                "verify-start",
                verification_status="running",
                verification_token="execution:1",
            ),
        )
        state, finished = self.controller.decide(
            state,
            AcceptanceObservation(
                "verify-finish",
                verification_status="passed",
                verification_token="execution:1",
            ),
        )

        self.assertEqual(first.action, "continue")
        self.assertEqual(finished.action, "complete")
        self.assertEqual(state["verified_revision"], 0)

    def test_consecutive_no_progress_blocks_and_recommends_recovery(self):
        state = None
        for index in range(3):
            state, decision = self.controller.decide(
                state,
                AcceptanceObservation(f"idle-{index}"),
            )

        self.assertEqual(decision.action, "blocked")
        self.assertTrue(decision.recovery_recommended)
        self.assertEqual(state["no_progress_count"], 3)

    def test_artifact_progress_resets_no_progress_counter(self):
        state, _ = self.controller.decide(None, AcceptanceObservation("idle-1"))
        state, _ = self.controller.decide(state, AcceptanceObservation("idle-2"))
        state, decision = self.controller.decide(
            state,
            AcceptanceObservation("build-1", artifacts_changed=True),
        )

        self.assertEqual(decision.action, "verify")
        self.assertEqual(state["no_progress_count"], 0)

    def test_exhausted_budget_blocks_without_recovery(self):
        _, decision = self.controller.decide(
            None,
            AcceptanceObservation("budget-1", remaining_budget_seconds=0),
        )

        self.assertEqual(decision.action, "blocked")
        self.assertFalse(decision.recovery_recommended)
        self.assertIn("exhausted", decision.reason)

    def test_same_observation_is_idempotent_across_json_restore(self):
        observation = AcceptanceObservation("build-1", artifacts_changed=True)
        state, first = self.controller.decide(None, observation)
        restored = json.loads(json.dumps(state))
        replayed, second = self.controller.decide(restored, observation)

        self.assertEqual(replayed, state)
        self.assertEqual(second, first)
        self.assertEqual(replayed["artifact_revision"], 1)
        self.assertEqual(replayed["observation_count"], 1)

    def test_reusing_observation_id_with_different_payload_is_rejected(self):
        state, _ = self.controller.decide(None, AcceptanceObservation("event-1"))

        with self.assertRaisesRegex(ValueError, "reused"):
            self.controller.decide(
                state,
                AcceptanceObservation("event-1", artifacts_changed=True),
            )

    def test_concrete_verification_requires_stable_token(self):
        with self.assertRaisesRegex(ValueError, "verification_token"):
            AcceptanceObservation("verify-1", verification_status="passed")

    def test_functional_api_returns_json_serializable_values(self):
        state, decision = advance_acceptance_progress(
            create_acceptance_progress_state(),
            AcceptanceObservation(
                "verify-1",
                verification_status="passed",
                verification_token="trace:1",
            ),
        )

        encoded = json.dumps({"state": state, "decision": decision})
        self.assertIn('"complete"', encoded)

    def test_compact_summary_counts_distinct_trace_evidence(self):
        state, decision = self.controller.decide(
            None,
            AcceptanceObservation(
                "verify-1",
                artifacts_changed=True,
                verification_status="passed",
                verification_token="trace:1",
            ),
        )
        summary = build_acceptance_summary(
            state,
            {
                "verification": {
                    "attempt_count_total": 4,
                    "stale_count_total": 3,
                    "attempts": [
                        {"token": "trace:1", "stale": False},
                        {"token": "trace:1", "stale": True},
                        {"token": "trace:2", "stale": True},
                    ]
                }
            },
            decision,
            repair_rounds=2,
        )

        self.assertEqual(summary, {
            "artifact_revisions": 1,
            "verification_attempts": 4,
            "stale_verifications": 3,
            "repair_rounds": 2,
            "final_accepted_revision": 1,
            "completion_reason": "verification passed for current artifact revision 1",
        })


if __name__ == "__main__":
    unittest.main()
