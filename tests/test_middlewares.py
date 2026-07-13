import unittest

from middlewares import BrowserTestBudgetMiddleware


class BrowserTestBudgetMiddlewareTests(unittest.TestCase):
    def test_warns_at_soft_limit_once(self):
        middleware = BrowserTestBudgetMiddleware(soft_limit=2, hard_limit=4)

        self.assertIsNone(middleware.post_tool("browser_test", {}, "ok", []))
        warning = middleware.post_tool("browser_test", {}, "ok", [])
        repeated = middleware.post_tool("browser_test", {}, "ok", [])

        self.assertIn("enough browser evidence", warning)
        self.assertIsNone(repeated)

    def test_blocks_at_hard_limit(self):
        middleware = BrowserTestBudgetMiddleware(soft_limit=2, hard_limit=3)

        middleware.post_tool("browser_test", {}, "ok", [])
        middleware.post_tool("browser_test", {}, "ok", [])
        warning = middleware.post_tool("browser_test", {}, "ok", [])

        self.assertIn("Browser test budget exhausted", warning)
        self.assertIn("write feedback.md", warning)

    def test_ignores_other_tools(self):
        middleware = BrowserTestBudgetMiddleware(soft_limit=1, hard_limit=2)

        self.assertIsNone(middleware.post_tool("read_file", {}, "ok", []))
        self.assertEqual(middleware.browser_test_count, 0)


if __name__ == "__main__":
    unittest.main()
