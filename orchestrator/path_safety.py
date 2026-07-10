from __future__ import annotations

from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a requested path escapes its workspace boundary."""


def resolve_workspace_path(workspace: str | Path, requested: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.expanduser().resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(f"path escapes workspace: {requested}") from exc

    return candidate
