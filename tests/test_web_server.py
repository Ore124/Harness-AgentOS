import importlib.util
import tempfile
import unittest
from pathlib import Path


@unittest.skipIf(importlib.util.find_spec("fastapi") is None, "fastapi is not installed")
class WebServerTests(unittest.TestCase):
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
            finally:
                config.WORKSPACE = old_workspace
                server.start_worker = old_start_worker


if __name__ == "__main__":
    unittest.main()
