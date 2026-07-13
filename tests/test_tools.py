import tempfile
import time
import urllib.error
import unittest
from pathlib import Path
from unittest import mock


class ToolTests(unittest.TestCase):
    def setUp(self):
        import tools

        tools._processes_by_run.clear()
        tools._dev_server_procs_by_run.clear()

    def test_cleanup_only_terminates_processes_registered_to_requested_run(self):
        import tools

        first = mock.Mock()
        first.poll.return_value = None
        second = mock.Mock()
        second.poll.return_value = None
        external = mock.Mock()
        external.poll.return_value = None
        tools._processes_by_run["first"] = [first]
        tools._processes_by_run["second"] = [second]

        with mock.patch.object(tools, "_terminate_process_tree") as terminate:
            result = tools.cleanup_run_processes("first")

        terminate.assert_called_once_with(first)
        self.assertNotIn("first", tools._processes_by_run)
        self.assertEqual(tools._processes_by_run["second"], [second])
        self.assertFalse(terminate.call_args.args[0] is external)
        self.assertEqual(result, "Stopped 1 process(es) for run first")

    def test_background_process_is_registered_under_active_run(self):
        from orchestrator.run_context import RunContext
        from pathlib import Path
        import tools

        proc = mock.Mock()
        proc.pid = 42
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(tools.subprocess, "Popen", return_value=proc), \
             mock.patch.object(tools.time, "sleep"):
            context = RunContext("run-a", Path(tmp), Path(tmp) / ".harness" / "traces")
            with context.activate():
                result = tools._start_background_command("python -m http.server")

        self.assertIn("Started background command", result)
        self.assertEqual(tools._processes_by_run["run-a"], [proc])

    def test_timed_out_foreground_command_terminates_only_its_registered_process_tree(self):
        from orchestrator.run_context import RunContext
        from pathlib import Path
        import tools

        proc = mock.Mock()
        proc.pid = 99
        proc.communicate.side_effect = tools.subprocess.TimeoutExpired("cmd", 1)
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(tools.subprocess, "Popen", return_value=proc), \
             mock.patch.object(tools, "_terminate_process_tree") as terminate:
            context = RunContext("run-a", Path(tmp), Path(tmp) / ".harness" / "traces")
            with context.activate():
                result = tools.run_bash("python -c \"import time; time.sleep(10)\"", timeout=1)

        self.assertIn("Command timed out", result)
        terminate.assert_called_once_with(proc)
        self.assertNotIn("run-a", tools._processes_by_run)

    def test_run_bash_rejects_taskkill_python_image(self):
        import tools

        result = tools.run_bash("taskkill /f /im python.exe")

        self.assertIn("Refusing to run broad Python process-kill command", result)
        self.assertIn("stop_background_commands", result)

    def test_run_bash_rejects_pkill_python_pattern(self):
        import tools

        result = tools.run_bash("pkill -f python")

        self.assertIn("Refusing to run broad Python process-kill command", result)
        self.assertIn("specific PID", result)

    def test_run_bash_allows_ordinary_command(self):
        import config
        import tools

        old_workspace = config.WORKSPACE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                result = tools.run_bash("python -c \"print('ok')\"")
                self.assertEqual(result, "ok")
            finally:
                config.WORKSPACE = old_workspace

    def test_run_bash_allows_non_executed_kill_text(self):
        import config
        import tools

        old_workspace = config.WORKSPACE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                result = tools.run_bash("python -c \"print('pkill -f python')\"")
                self.assertEqual(result, "pkill -f python")
            finally:
                config.WORKSPACE = old_workspace

    def test_run_bash_executes_unix_inspection_through_bash(self):
        import config
        import tools

        old_workspace = config.WORKSPACE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                Path(tmp, "pomodoro-timer.html").write_text("<html>ok</html>", encoding="utf-8")

                result = tools.run_bash("pwd && ls -la pomodoro-timer.html")

                self.assertIn("pomodoro-timer.html", result)
                self.assertNotIn("不是内部或外部命令", result)
            finally:
                config.WORKSPACE = old_workspace

    def test_shell_invocation_prefers_bash_when_available(self):
        import tools

        with mock.patch.object(tools, "_bash_executable", return_value="/bin/bash"):
            popen_args, use_shell = tools._shell_invocation("pwd && ls -la")

        self.assertEqual(popen_args, ["/bin/bash", "-lc", "pwd && ls -la"])
        self.assertFalse(use_shell)

    def test_shell_invocation_falls_back_to_platform_shell_without_bash(self):
        import tools

        with mock.patch.object(tools, "_bash_executable", return_value=None):
            popen_args, use_shell = tools._shell_invocation("dir")

        self.assertEqual(popen_args, "dir")
        self.assertTrue(use_shell)

    def test_read_file_rejects_screenshot_binary(self):
        import config
        import tools

        old_workspace = config.WORKSPACE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                Path(tmp, "_screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 128)

                result = tools.read_file("_screenshot.png")

                self.assertIn("Refusing to read image file as text", result)
                self.assertIn("browser_test text report", result)
                self.assertNotIn("PNG", result[80:])
            finally:
                config.WORKSPACE = old_workspace

    def test_read_file_rejects_binary_content_without_known_extension(self):
        import config
        import tools

        old_workspace = config.WORKSPACE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                Path(tmp, "payload.dat").write_bytes(b"abc\x00def" * 20)

                result = tools.read_file("payload.dat")

                self.assertIn("Refusing to read binary file as text", result)
            finally:
                config.WORKSPACE = old_workspace

    def test_stop_background_commands_uses_internal_pid_cleanup(self):
        import tools

        proc = mock.Mock()
        proc.poll.return_value = None
        tools._background_procs.append(proc)

        with mock.patch.object(tools, "_terminate_process_tree") as terminate:
            result = tools.stop_background_commands()

        terminate.assert_called_once_with(proc)
        self.assertEqual(result, "Stopped 1 background command(s)")

    def test_run_bash_trailing_ampersand_starts_background_process(self):
        import config
        import tools

        old_workspace = config.WORKSPACE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                started = time.time()
                result = tools.run_bash(
                    "python -c \"import time; time.sleep(5)\" &",
                    timeout=1,
                )
                elapsed = time.time() - started
                self.assertLess(elapsed, 2)
                self.assertIn("Started background command", result)
            finally:
                config.WORKSPACE = old_workspace
                tools.stop_background_commands()

    def test_run_bash_http_server_starts_background_process(self):
        import config
        import tools

        old_workspace = config.WORKSPACE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                started = time.time()
                result = tools.run_bash("python -m http.server 8766", timeout=1)
                elapsed = time.time() - started
                self.assertLess(elapsed, 2)
                self.assertIn("Started background command", result)
            finally:
                config.WORKSPACE = old_workspace
                tools.stop_background_commands()

    def test_browser_preflight_fails_fast_when_server_unreachable(self):
        import tools

        error = urllib.error.URLError("connection refused")
        with mock.patch.object(tools.urllib.request, "urlopen", side_effect=error):
            result = tools._preflight_http_url("http://localhost:8000/pomodoro-timer.html")

        self.assertIn("Browser preflight failed", result)
        self.assertIn("start_command", result)
        self.assertIn("correct port", result)

    def test_browser_preflight_allows_http_error_pages(self):
        import tools

        error = urllib.error.HTTPError(
            "http://localhost:8000/missing.html",
            404,
            "Not Found",
            hdrs=None,
            fp=None,
        )
        with mock.patch.object(tools.urllib.request, "urlopen", side_effect=error):
            result = tools._preflight_http_url("http://localhost:8000/missing.html")

        self.assertIsNone(result)

    def test_browser_test_stops_when_dev_server_start_fails(self):
        import tools

        with mock.patch.object(tools, "HAS_PLAYWRIGHT", True), \
             mock.patch.object(tools, "_ensure_dev_server", return_value="[error] boom"), \
             mock.patch.object(tools, "_preflight_http_url") as preflight, \
             mock.patch.object(tools, "sync_playwright") as playwright:
            result = tools.browser_test(
                "http://localhost:8000/pomodoro-timer.html",
                start_command="python -m http.server 8000",
            )

        self.assertIn("Server: [error] boom", result)
        preflight.assert_not_called()
        playwright.assert_not_called()

    def test_browser_test_auto_starts_static_server_for_local_html(self):
        import config
        import tools

        old_workspace = config.WORKSPACE
        with tempfile.TemporaryDirectory() as tmp:
            try:
                config.WORKSPACE = tmp
                Path(tmp, "pomodoro-timer.html").write_text("<html>ok</html>", encoding="utf-8")
                with mock.patch.object(tools, "HAS_PLAYWRIGHT", True), \
                     mock.patch.object(tools, "_preflight_http_url", side_effect=[
                         "[error] Browser preflight failed: cannot reach http://localhost:8000/pomodoro-timer.html within 2s.",
                         None,
                     ]) as preflight, \
                     mock.patch.object(tools, "_ensure_dev_server", return_value="Dev server started (pid=1, port=8000)") as ensure, \
                     mock.patch.object(tools, "sync_playwright", side_effect=RuntimeError("stop after preflight")):
                    result = tools.browser_test("http://localhost:8000/pomodoro-timer.html", screenshot=False)

                ensure.assert_called_once_with("python -m http.server 8000", 8000, 2)
                self.assertEqual(preflight.call_count, 2)
                self.assertIn("Server: Dev server started", result)
                self.assertIn("Browser test failed", result)
            finally:
                config.WORKSPACE = old_workspace


if __name__ == "__main__":
    unittest.main()
