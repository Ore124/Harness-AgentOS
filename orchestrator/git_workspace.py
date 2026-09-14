"""Git worktree isolation and serialized integration."""
from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class PreparedWorkspace:
    path: Path
    branch: str
    base_revision: str


@dataclass(frozen=True)
class IntegrationResult:
    succeeded: bool
    commit: str | None = None
    conflict_output: str = ""


class GitWorktreeManager:
    def __init__(self, repository: str | Path, worktree_root: str | Path, *, runner=subprocess.run):
        self.repository = Path(repository).expanduser().resolve()
        self.worktree_root = Path(worktree_root).expanduser().resolve()
        self.runner = runner
        self._integration_lock = threading.RLock()

    def prepare(self, work_item: Mapping[str, Any], base_revision: str) -> PreparedWorkspace:
        workspace = self.resolve(work_item, base_revision)
        if workspace.path.exists():
            self._run(["git", "rev-parse", "--is-inside-work-tree"], cwd=workspace.path)
            return workspace
        workspace.path.parent.mkdir(parents=True, exist_ok=True)
        branch_exists = self.runner(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{workspace.branch}"],
            cwd=str(self.repository),
            capture_output=True,
            text=True,
            shell=False,
        ).returncode == 0
        command = ["git", "worktree", "add"]
        if not branch_exists:
            command.extend(["-b", workspace.branch])
        command.append(str(workspace.path))
        command.append(workspace.branch if branch_exists else base_revision)
        self._run(command, cwd=self.repository)
        return workspace

    def resolve(self, work_item: Mapping[str, Any], base_revision: str) -> PreparedWorkspace:
        run_id = _safe_ref(str(work_item.get("run_id", "run")))
        item_id = _safe_ref(str(work_item.get("id", "work")))
        branch = f"harness/{run_id}/{item_id}"
        path = (self.worktree_root / run_id / item_id).resolve()
        if not path.is_relative_to(self.worktree_root):
            raise ValueError("worktree path escaped configured root")
        return PreparedWorkspace(path, branch, base_revision)

    def commit(self, workspace: PreparedWorkspace, message: str) -> str:
        self._run(["git", "add", "-A"], cwd=workspace.path)
        self._run(["git", "commit", "-m", message, "--allow-empty"], cwd=workspace.path)
        return self._run(["git", "rev-parse", "HEAD"], cwd=workspace.path).stdout.strip()

    def head(self, workspace: PreparedWorkspace) -> str:
        return self._run(["git", "rev-parse", "HEAD"], cwd=workspace.path).stdout.strip()

    def changed_files(self, workspace: PreparedWorkspace) -> tuple[str, ...]:
        output = self._run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace.path,
        ).stdout
        return tuple(
            line[3:].strip().replace("\\", "/")
            for line in output.splitlines()
            if len(line) >= 4
        )

    def integrate(self, workspace: PreparedWorkspace, target_branch: str) -> IntegrationResult:
        with self._integration_lock:
            self._run(["git", "switch", target_branch], cwd=self.repository)
            result = self.runner(
                ["git", "merge", "--no-ff", workspace.branch, "-m", f"Integrate {workspace.branch}"],
                cwd=str(self.repository),
                capture_output=True,
                text=True,
                shell=False,
            )
            if result.returncode != 0:
                conflict = (str(result.stdout) + "\n" + str(result.stderr))[-20000:]
                self.runner(
                    ["git", "merge", "--abort"],
                    cwd=str(self.repository),
                    capture_output=True,
                    text=True,
                    shell=False,
                )
                return IntegrationResult(False, conflict_output=conflict)
            commit = self._run(["git", "rev-parse", "HEAD"], cwd=self.repository).stdout.strip()
            return IntegrationResult(True, commit=commit)

    def remove(self, workspace: PreparedWorkspace) -> None:
        if not workspace.path.is_relative_to(self.worktree_root):
            raise ValueError("refusing to remove worktree outside configured root")
        self._run(
            ["git", "worktree", "remove", "--force", str(workspace.path)],
            cwd=self.repository,
        )

    def _run(self, command: list[str], *, cwd: Path):
        result = self.runner(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            shell=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git command failed ({result.returncode}): "
                + (str(result.stderr) or str(result.stdout))[-4000:]
            )
        return result


def _safe_ref(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not normalized or normalized in {"HEAD", "@"}:
        raise ValueError("invalid git work identifier")
    return normalized[:100]
