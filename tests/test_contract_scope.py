import tempfile
import unittest
from pathlib import Path

import config
from agents import AgentRunResult
from harness import Harness
from orchestrator.scheduler import PhaseExecutionError


class ContractScopeTests(unittest.TestCase):
    def test_contract_agent_terminal_failure_propagates_to_scheduler(self):
        harness = Harness.__new__(Harness)
        harness.contract_proposer = FailingContractAgent()
        harness.contract_reviewer = FakeContractAgent(approve=True)

        with self.assertRaises(PhaseExecutionError):
            harness._negotiate_contract(1)

    def test_contract_negotiation_includes_original_user_request(self):
        old_workspace = config.WORKSPACE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                harness = Harness.__new__(Harness)
                harness.contract_proposer = FakeContractAgent()
                harness.contract_reviewer = FakeContractAgent(approve=True)

                harness._negotiate_contract(
                    1,
                    user_prompt="Build a Pomodoro timer. Single HTML file.",
                )

                self.assertIn("Original user request", harness.contract_proposer.prompts[0])
                self.assertIn("Single HTML file", harness.contract_proposer.prompts[0])
                self.assertIn("Original user request", harness.contract_reviewer.prompts[0])
                self.assertIn("Single HTML file", harness.contract_reviewer.prompts[0])
            finally:
                config.WORKSPACE = old_workspace


class FakeContractAgent:
    def __init__(self, approve=False):
        self.approve = approve
        self.prompts = []

    def run(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        contract = "APPROVED\n" if self.approve else ""
        contract += "## Sprint Contract\n"
        Path(config.WORKSPACE, "contract.md").write_text(contract, encoding="utf-8")
        return "ok"


class FailingContractAgent:
    def run(self, _prompt, **_kwargs):
        return AgentRunResult("", "api_errors", 5)


if __name__ == "__main__":
    unittest.main()
