import unittest

from orchestrator.verification import is_verification_command


class VerificationCommandTests(unittest.TestCase):
    def test_recognizes_windows_py_test_commands(self):
        for command in (
            "py -m pytest -q",
            "py.exe -m unittest discover",
            "py -3.12 -m pytest tests/test_api.py",
        ):
            with self.subTest(command=command):
                self.assertTrue(is_verification_command(command))

    def test_recognizes_inline_python_checks_on_windows_and_linux(self):
        for command in (
            'py -c "assert 2 + 2 == 4"',
            'py -3.12 -c "assert 2 + 2 == 4"',
            "python -c 'import sys; sys.exit(0)'",
            "python3 -c 'assert True'",
            "python -c 'raise SystemExit(0)'",
            "py -c 'unittest.main()'",
        ):
            with self.subTest(command=command):
                self.assertTrue(is_verification_command(command))

    def test_rejects_inline_python_without_machine_check_semantics(self):
        for command in (
            'py -c "print(2 + 2)"',
            'py -c "print(\'assert True\')"',
            "python -c 'from pathlib import Path; print(Path.cwd())'",
            "py -3.12 -c 'value = 4'",
        ):
            with self.subTest(command=command):
                self.assertFalse(is_verification_command(command))

    def test_keeps_non_verification_commands_excluded(self):
        for command in ("py app.py", "python script.py", "ls -la", "cat app.py"):
            with self.subTest(command=command):
                self.assertFalse(is_verification_command(command))


if __name__ == "__main__":
    unittest.main()
