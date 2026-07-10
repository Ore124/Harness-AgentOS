import tempfile
import unittest
from pathlib import Path


class PathSafetyCharacterizationTests(unittest.TestCase):
    def test_sibling_prefix_path_must_not_be_allowed(self):
        from tools import _resolve

        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "run"
            sibling = Path(root) / "run-escape"
            workspace.mkdir()
            sibling.mkdir()

            import config

            old_workspace = config.WORKSPACE
            config.WORKSPACE = str(workspace)
            try:
                with self.assertRaises(Exception):
                    _resolve("../run-escape/secret.txt")
            finally:
                config.WORKSPACE = old_workspace

    def test_normal_relative_path_stays_inside_workspace(self):
        from tools import _resolve

        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "run"
            workspace.mkdir()

            import config

            old_workspace = config.WORKSPACE
            config.WORKSPACE = str(workspace)
            try:
                resolved = _resolve("logs/output.txt")
                self.assertTrue(str(resolved).endswith(str(Path("logs") / "output.txt")))
            finally:
                config.WORKSPACE = old_workspace
