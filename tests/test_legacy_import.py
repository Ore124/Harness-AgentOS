import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.legacy_import import import_legacy_state
from orchestrator.store import OrchestratorStore
from orchestrator.workflow_repository import SqlWorkflowRepository


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.repository = SqlWorkflowRepository("sqlite://")

    def tearDown(self):
        self.repository.close()

    def _write(self, root, **overrides):
        state = {
            "run_id": "old-1",
            "prompt": "ship",
            "profile": "terminal",
            "workspace": str(Path(root) / "old-workspace"),
            "phase": "analyze",
            "status": "completed",
            "validation": {"status": "verified"},
            "artifacts": {"files": []},
        }
        state.update(overrides)
        path = Path(root) / "harness_state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def test_verified_completed_state_imports_once_and_remains_completed(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._write(root)
            first = import_legacy_state(path, self.repository)
            second = import_legacy_state(path, self.repository)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["verified_revision"], 1)
        self.assertEqual(first["evidence"][0]["kind"], "legacy_state_import")

    def test_unverified_legacy_completion_is_not_false_completed(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._write(
                root,
                phase="analyze",
                status="completed",
                validation={"status": "missing"},
            )
            imported = import_legacy_state(path, self.repository)
        self.assertNotEqual(imported["status"], "completed")
        self.assertIsNone(imported["verified_revision"])

    def test_import_captures_legacy_sqlite_and_canonical_trace_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "old-workspace"
            harness_dir = workspace / ".harness"
            harness_dir.mkdir(parents=True)
            store = OrchestratorStore(harness_dir / "orchestrator.db")
            store.append_event("old-1", {"type": "phase_started"})
            trace = [
                {
                    "schema_version": 1,
                    "event_id": "start",
                    "run_id": "old-1",
                    "seq": 1,
                    "ts_ms": 1,
                    "event_type": "run_started",
                    "role": None,
                    "phase": None,
                    "payload": {},
                },
                {
                    "schema_version": 1,
                    "event_id": "complete",
                    "run_id": "old-1",
                    "seq": 2,
                    "ts_ms": 2,
                    "event_type": "run_completed",
                    "role": None,
                    "phase": None,
                    "payload": {"status": "completed", "task_success": True},
                },
            ]
            (harness_dir / "canonical_trace.jsonl").write_text(
                "\n".join(json.dumps(item) for item in trace) + "\n",
                encoding="utf-8",
            )
            path = self._write(root, workspace=str(workspace))
            imported = import_legacy_state(path, self.repository)
        kinds = {item["kind"] for item in imported["evidence"]}
        self.assertIn("legacy_sqlite_store_import", kinds)
        self.assertIn("legacy_canonical_trace_import", kinds)


if __name__ == "__main__":
    unittest.main()
