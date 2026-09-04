"""Shared helpers for recognizing concrete verification actions.

Verification is intentionally narrower than "a command was executed".  The
completion gate should accept tests and machine-checkable assertions, but not
exploratory commands such as ``ls`` or ``cat`` on their own.
"""
from __future__ import annotations

import ast
import re
from typing import Any


# Accept both POSIX Python executables and the Windows ``py`` launcher.  The
# latter may include an interpreter selector (for example ``py -3.12``).
_PYTHON_LAUNCHER = (
    r"(?:python(?:3(?:\.\d+)?)?(?:\.exe)?|"
    r"py(?:\.exe)?(?:\s+-\d(?:\.\d+)?)?)"
)

_PYTHON_INLINE_COMMAND = re.compile(
    rf"(?:^|[;&|]\s*){_PYTHON_LAUNCHER}\s+-c\s+"
    r"(?P<code>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^;&|]+)",
    re.IGNORECASE,
)

_VERIFICATION_PATTERNS = (
    # Common test runners and test targets.
    re.compile(
        rf"(?:^|[;&|]\s*)(?:{_PYTHON_LAUNCHER}\s+-m\s+)?"
        r"(?:pytest|unittest|nose2|tox|nox)(?:\s|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test(?:\s|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[;&|]\s*)(?:cargo|go|dotnet|mvn|gradle|mix)\s+test(?:\s|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[;&|]\s*)(?:make|cmake\s+--build\s+\S+\s+--target)\s+"
        r"(?:test|tests|check)(?:\s|$)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|[;&|]\s*)ctest(?:\s|$)", re.IGNORECASE),
    # Project-provided test/check/verify scripts and binaries.
    re.compile(
        rf"(?:^|[;&|]\s*)(?:bash|sh|{_PYTHON_LAUNCHER}|node)?\s*"
        r"(?:\./)?[^\s;&|]*(?:test|check|verify)[^\s;&|]*"
        r"(?:\s|$)",
        re.IGNORECASE,
    ),
    # Shell assertions and exact-output comparisons.
    re.compile(r"(?:^|[;&|]\s*)(?:test\s+|\[\[\s+|\[\s+)", re.IGNORECASE),
    re.compile(r"(?:^|[;&|]\s*)(?:diff|cmp)(?:\s|$)", re.IGNORECASE),
    # Syntax/type/static checks that have process exit semantics.
    re.compile(
        rf"(?:^|[;&|]\s*)(?:{_PYTHON_LAUNCHER}\s+-m\s+)?"
        r"(?:compileall|py_compile|mypy|ruff|flake8|pylint|shellcheck|eslint|tsc)"
        r"(?:\s|$)",
        re.IGNORECASE,
    ),
)


def _is_python_inline_check(command: str) -> bool:
    """Recognize ``python -c`` only when its code makes a real assertion."""
    for match in _PYTHON_INLINE_COMMAND.finditer(command):
        code = match.group("code")
        if len(code) >= 2 and code[0] == code[-1] and code[0] in {"'", '"'}:
            code = code[1:-1]
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        if any(_is_inline_check_node(node) for node in ast.walk(tree)):
            return True
    return False


def _is_inline_check_node(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.Raise) and isinstance(node.exc, (ast.Name, ast.Call)):
        target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        return isinstance(target, ast.Name) and target.id in {"AssertionError", "SystemExit"}
    if not isinstance(node, ast.Call):
        return False

    target = node.func
    if isinstance(target, ast.Name):
        return target.id.lower().startswith("test") or target.id == "SystemExit"
    if not isinstance(target, ast.Attribute):
        return False

    parts = [target.attr]
    value = target.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    qualified = ".".join(reversed(parts)).lower()
    return (
        qualified == "sys.exit"
        or qualified.startswith("pytest.")
        or qualified.startswith("unittest.")
        or target.attr.lower().startswith("test")
    )


def is_verification_command(command: str) -> bool:
    """Return whether *command* provides machine-checkable verification."""
    normalized = str(command or "").strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _VERIFICATION_PATTERNS) or (
        _is_python_inline_check(normalized)
    )


def is_verification_tool_call(tool_name: str, arguments: dict[str, Any] | None) -> bool:
    """Return whether a tool invocation is concrete verification evidence."""
    if tool_name == "browser_test":
        return True
    if tool_name != "run_bash":
        return False
    return is_verification_command(str((arguments or {}).get("command", "")))
