import unittest

from middlewares import BrowserTestBudgetMiddleware
from profiles.app_builder import AppBuilderProfile


class AppBuilderProfileTests(unittest.TestCase):
    def test_evaluator_has_browser_budget_and_time_budget(self):
        profile = AppBuilderProfile()
        config = profile.evaluator()

        self.assertTrue(any(isinstance(mw, BrowserTestBudgetMiddleware) for mw in config.middlewares))
        self.assertEqual(config.time_budget, 180)

    def test_planner_and_contract_agents_have_time_budgets(self):
        profile = AppBuilderProfile()

        self.assertEqual(profile.planner().time_budget, 90)
        self.assertEqual(profile.contract_proposer().time_budget, 90)
        self.assertEqual(profile.contract_reviewer().time_budget, 90)


if __name__ == "__main__":
    unittest.main()
