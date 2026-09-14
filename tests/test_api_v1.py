import unittest

from fastapi.testclient import TestClient

from orchestrator.security import AuthContext, Authenticator, AuthorizationError, authorize_project, redact
from orchestrator.workflow_repository import SqlWorkflowRepository
from web.server import app


class DurableApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = SqlWorkflowRepository("sqlite://")
        app.state.workflow_repository = self.repository
        app.state.authenticator = Authenticator("disabled")
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.repository.close()
        del app.state.workflow_repository
        del app.state.authenticator

    def test_create_requires_idempotency_key_and_reuses_same_run(self):
        payload = {
            "project_id": "p1",
            "goal": "ship safely",
            "profile": "terminal",
            "max_total_seconds": 180,
        }
        missing = self.client.post("/api/v1/runs", json=payload)
        self.assertEqual(missing.status_code, 400)

        first = self.client.post(
            "/api/v1/runs", json=payload, headers={"Idempotency-Key": "request-1"}
        )
        second = self.client.post(
            "/api/v1/runs", json=payload, headers={"Idempotency-Key": "request-1"}
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["run_id"], second.json()["run_id"])
        self.assertEqual(first.json()["work_item_statuses"]["plan"], "ready")

    def test_graph_pause_resume_and_cancel(self):
        created = self.client.post(
            "/api/v1/runs",
            json={
                "project_id": "p1",
                "goal": "ship safely",
                "profile": "terminal",
                "max_total_seconds": 180,
            },
            headers={"Idempotency-Key": "request-2"},
        ).json()
        run_id = created["run_id"]
        graph = self.client.get(f"/api/v1/runs/{run_id}/graph")
        self.assertEqual(graph.status_code, 200)
        self.assertEqual([node["id"] for node in graph.json()["nodes"]], ["plan", "build", "verify"])

        self.assertEqual(
            self.client.post(f"/api/v1/runs/{run_id}/pause").json()["status"],
            "paused",
        )
        self.assertEqual(
            self.client.post(f"/api/v1/runs/{run_id}/resume").json()["status"],
            "running",
        )
        cancelled = self.client.post(f"/api/v1/runs/{run_id}/cancel").json()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(set(cancelled["work_item_statuses"].values()), {"cancelled"})

    def test_health_reports_durable_database(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["runtime"], "durable")
        self.assertIn("queue", response.json())

    def test_project_audit_and_queue_are_queryable(self):
        self.client.post(
            "/api/v1/runs",
            json={
                "project_id": "p1",
                "goal": "observable run",
                "profile": "terminal",
                "max_total_seconds": 180,
            },
            headers={"Idempotency-Key": "audit-request"},
        )
        audit = self.client.get("/api/v1/projects/p1/audit")
        self.assertEqual(audit.status_code, 200)
        self.assertGreaterEqual(len(audit.json()["events"]), 3)
        queue = self.client.get("/api/v1/projects/p1/queue")
        self.assertEqual(queue.status_code, 200)
        self.assertEqual(queue.json()["ready"], 1)


class SecurityTests(unittest.TestCase):
    def test_project_membership_is_authoritative(self):
        repository = SqlWorkflowRepository("sqlite://")
        try:
            repository.set_membership("p1", "alice", "viewer")
            context = AuthContext("alice", {"sub": "alice"})
            self.assertEqual(authorize_project(context, repository, "p1", "read"), "viewer")
            with self.assertRaises(AuthorizationError):
                authorize_project(context, repository, "p1", "control_run")
        finally:
            repository.close()

    def test_recursive_redaction_removes_secret_fields(self):
        value = redact({"nested": {"api_key": "secret", "safe": [1, {"token": "x"}]}})
        self.assertEqual(value["nested"]["api_key"], "[redacted]")
        self.assertEqual(value["nested"]["safe"][1]["token"], "[redacted]")


if __name__ == "__main__":
    unittest.main()
