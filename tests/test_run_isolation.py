import tempfile
import unittest
from pathlib import Path


class RunIsolationCharacterizationTests(unittest.TestCase):
    def test_workspace_global_can_be_changed_between_runs(self):
        import config

        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "first"
            second = Path(root) / "second"
            first.mkdir()
            second.mkdir()

            old_workspace = config.WORKSPACE
            try:
                config.WORKSPACE = str(first)
                self.assertEqual(Path(config.WORKSPACE), first)
                config.WORKSPACE = str(second)
                self.assertEqual(Path(config.WORKSPACE), second)
            finally:
                config.WORKSPACE = old_workspace
