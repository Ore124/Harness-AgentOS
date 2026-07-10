import importlib.util
import tempfile
import unittest
from pathlib import Path


@unittest.skipIf(importlib.util.find_spec("fastapi") is None, "fastapi is not installed")
class WebServerTests(unittest.TestCase):
    def test_event_cursor_returns_only_new_events(self):
        from orchestrator.store import OrchestratorStore

        with tempfile.TemporaryDirectory() as root:
            store = OrchestratorStore(Path(root) / "state.db")
            store.save_state({"run_id": "r1", "workspace": root, "status": "running"})
            store.append_event("r1", {"type": "one"})
            first = store.list_events("r1")
            store.append_event("r1", {"type": "two"})
            second = store.list_events("r1", after_id=first[-1]["_event_id"])

        self.assertEqual([event["type"] for event in second], ["two"])

    def test_terminal_endpoint_disabled_by_default(self):
        from fastapi.testclient import TestClient
        from web.server import app

        response = TestClient(app).post("/api/terminal/run", json={"command": "echo hi"})
        self.assertEqual(response.status_code, 403)

    def test_create_pause_resume_run(self):
        import config
        import web.server as server
        from fastapi.testclient import TestClient
        from web.server import app

        old_workspace = config.WORKSPACE
        old_start_worker = server.start_worker
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                server.start_worker = lambda _run_id: None
                client = TestClient(app)

                response = client.post("/api/runs", json={"prompt": "Build a web app", "profile": "auto"})
                self.assertEqual(response.status_code, 200)
                state = response.json()
                run_id = state["run_id"]
                self.assertTrue((Path(tmp) / run_id / "harness_state.json").exists())

                pause = client.post(f"/api/runs/{run_id}/pause")
                self.assertEqual(pause.status_code, 200)
                self.assertFalse(pause.json()["active"])

                resume = client.post(f"/api/runs/{run_id}/resume")
                self.assertEqual(resume.status_code, 200)
                self.assertTrue(resume.json()["active"])

                state_path = Path(tmp) / run_id / "harness_state.json"
                import json
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["requires_human_approval"] = True
                state["human_approval"] = {"reason": "approve smoke"}
                state["status"] = "waiting_approval"
                state["active"] = False
                state_path.write_text(json.dumps(state), encoding="utf-8")

                approve = client.post(f"/api/runs/{run_id}/approve")
                self.assertEqual(approve.status_code, 200)
                self.assertFalse(approve.json()["requires_human_approval"])
            finally:
                config.WORKSPACE = old_workspace
                server.start_worker = old_start_worker

    def test_terminal_run_command_and_cd(self):
        import config
        import web.server as server
        from fastapi.testclient import TestClient
        from web.server import app

        old_workspace = config.WORKSPACE
        old_terminal_enabled = getattr(config, "WEB_TERMINAL_ENABLED", False)
        old_cwds = dict(server._terminal_cwds)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                config.WEB_TERMINAL_ENABLED = True
                client = TestClient(app)

                response = client.post("/api/terminal/run", json={
                    "command": "python -c \"print('terminal-ok')\"",
                    "session_id": "test-terminal",
                })
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["exit_code"], 0)
                self.assertIn("terminal-ok", payload["stdout"])

                response = client.post("/api/terminal/run", json={
                    "command": f"cd {tmp}",
                    "session_id": "test-terminal",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(Path(response.json()["cwd"]).resolve(), Path(tmp).resolve())
            finally:
                config.WORKSPACE = old_workspace
                config.WEB_TERMINAL_ENABLED = old_terminal_enabled
                server._terminal_cwds.clear()
                server._terminal_cwds.update(old_cwds)


if __name__ == "__main__":
    unittest.main()
