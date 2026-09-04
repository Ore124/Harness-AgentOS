import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_adapter_without_harbor():
    installed = types.ModuleType("harbor.agents.installed.base")
    environments = types.ModuleType("harbor.environments.base")
    contexts = types.ModuleType("harbor.models.agent.context")

    class BaseInstalledAgent:
        pass

    class BaseEnvironment:
        pass

    class AgentContext:
        pass

    installed.BaseInstalledAgent = BaseInstalledAgent
    installed.with_prompt_template = lambda fn: fn
    environments.BaseEnvironment = BaseEnvironment
    contexts.AgentContext = AgentContext
    modules = {
        "harbor": types.ModuleType("harbor"),
        "harbor.agents": types.ModuleType("harbor.agents"),
        "harbor.agents.installed": types.ModuleType("harbor.agents.installed"),
        "harbor.agents.installed.base": installed,
        "harbor.environments": types.ModuleType("harbor.environments"),
        "harbor.environments.base": environments,
        "harbor.models": types.ModuleType("harbor.models"),
        "harbor.models.agent": types.ModuleType("harbor.models.agent"),
        "harbor.models.agent.context": contexts,
    }
    sys.modules.pop("benchmarks.harbor_agent", None)
    with patch.dict(sys.modules, modules):
        adapter = importlib.import_module("benchmarks.harbor_agent")
    sys.modules.pop("benchmarks.harbor_agent", None)
    return adapter


class HarborAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = _load_adapter_without_harbor()

    def test_environment_name_supplies_current_harbor_task_identity(self):
        context = SimpleNamespace(metadata=None)
        environment = SimpleNamespace(environment_name="adaptive-rejection-sampler")

        self.assertEqual(
            self.adapter._extract_task_id(context, environment),
            "adaptive-rejection-sampler",
        )

    def test_context_metadata_takes_priority_over_environment_fallback(self):
        context = SimpleNamespace(metadata={"task_id": "metadata-task"})
        environment = SimpleNamespace(environment_name="environment-task")

        self.assertEqual(
            self.adapter._extract_task_id(context, environment),
            "metadata-task",
        )

    def test_local_source_staging_excludes_secrets_and_virtualenvs(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as dest_tmp:
            source = Path(source_tmp)
            destination = Path(dest_tmp) / "staged"
            (source / "harness.py").write_text("print('ok')", encoding="utf-8")
            (source / ".env").write_text("SECRET=value", encoding="utf-8")
            (source / "venv312").mkdir()
            (source / "venv312" / "python.exe").write_text("binary", encoding="utf-8")
            (source / "profiles").mkdir()
            (source / "profiles" / "terminal.py").write_text("PROFILE = 1", encoding="utf-8")

            self.adapter._stage_local_source(source, destination)

            self.assertTrue((destination / "harness.py").exists())
            self.assertTrue((destination / "profiles" / "terminal.py").exists())
            self.assertFalse((destination / ".env").exists())
            self.assertFalse((destination / "venv312").exists())


if __name__ == "__main__":
    unittest.main()
