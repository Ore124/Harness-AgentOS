import unittest

import prompts


class PromptCompositionTests(unittest.TestCase):
    def test_default_prompt_order_matches_original_behavior(self):
        result = prompts.compose_system_prompt("ROLE", "SKILLS", prefix_v2=False)

        self.assertEqual(result, "ROLESKILLS")

    def test_prefix_v2_places_stable_skill_catalog_first_without_extra_text(self):
        result = prompts.compose_system_prompt("ROLE", "SKILLS", prefix_v2=True)

        self.assertEqual(result, "SKILLS\nROLE")

    def test_empty_skill_catalog_returns_role_prompt(self):
        self.assertEqual(prompts.compose_system_prompt("ROLE", "", prefix_v2=True), "ROLE")

    def test_planner_prompt_contains_scope_guard(self):
        self.assertIn("preserve the user's requested scope exactly", prompts.PLANNER_SYSTEM)
        self.assertIn("Do not add AI-powered features unless the user explicitly asks for AI", prompts.PLANNER_SYSTEM)
        self.assertIn("single HTML file", prompts.PLANNER_SYSTEM)
        self.assertNotIn("Be ambitious about scope", prompts.PLANNER_SYSTEM)
        self.assertNotIn("weave AI-powered features", prompts.PLANNER_SYSTEM)

    def test_builder_prompt_forbids_extra_files_for_single_html_scope(self):
        self.assertIn("exactly one application .html file", prompts.BUILDER_SYSTEM)
        self.assertIn("do not write temporary test files", prompts.BUILDER_SYSTEM.lower())

    def test_contract_prompts_contain_scope_guard(self):
        self.assertIn("Do not expand the sprint beyond the original user request", prompts.CONTRACT_BUILDER_SYSTEM)
        self.assertIn("Bounded to the original user request", prompts.CONTRACT_REVIEWER_SYSTEM)


if __name__ == "__main__":
    unittest.main()
