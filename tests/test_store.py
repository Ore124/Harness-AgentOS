import tempfile
import unittest
from pathlib import Path


class OrchestratorStoreTests(unittest.TestCase):
    def test_save_load_and_append_events(self):
        from orchestrator.store import OrchestratorStore

        with tempfile.TemporaryDirectory() as root:
            store = OrchestratorStore(Path(root) / "state.db")
            state = {"run_id": "r1", "workspace": str(Path(root) / "run"), "status": "created"}

            store.save_state(state)
            store.append_event("r1", {"type": "created"})

            self.assertEqual(store.load_state("r1")["status"], "created")
            self.assertEqual(store.list_events("r1")[0]["type"], "created")
