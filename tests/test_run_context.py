import tempfile
import unittest
from pathlib import Path


class RunContextTests(unittest.TestCase):
    def test_context_uses_state_workspace_and_run_id(self):
        from orchestrator.run_context import RunContext

        with tempfile.TemporaryDirectory() as root:
            state = {"run_id": "abc123", "workspace": str(Path(root) / "run")}
            ctx = RunContext.from_state(state)

        self.assertEqual(ctx.run_id, "abc123")
        self.assertTrue(str(ctx.workspace).endswith("run"))
        self.assertTrue(str(ctx.trace_dir).endswith(str(Path("run") / ".harness" / "traces")))
        self.assertFalse(ctx.allow_terminal)
