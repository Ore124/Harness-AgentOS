"""Policy-controlled local and Docker effect executors."""
from __future__ import annotations

import os
import json
import subprocess
import tempfile
import time
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol


@dataclass(frozen=True)
class EffectRequest:
    effect_id: str
    command: tuple[str, ...]
    workspace: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    secret_refs: Mapping[str, str] = field(default_factory=dict)
    network_hosts: tuple[str, ...] = ()
    timeout_seconds: int = 600
    cancel_check: Callable[[], bool] | None = field(default=None, repr=False, compare=False)
    checkpoint_path: Path | None = None
    checkpoint_callback: Callable[[Mapping[str, object]], None] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class SandboxPolicy:
    image: str = "python:3.12-slim"
    cpu_limit: float = 2.0
    memory_limit: str = "2g"
    pids_limit: int = 256
    allow_network_hosts: tuple[str, ...] = ()
    egress_network: str | None = None
    egress_proxy_url: str | None = None
    allowed_images: tuple[str, ...] = ("python:3.12-slim",)
    allowed_workspace_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ExecutionResult:
    effect_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class SecretProvider(Protocol):
    def resolve(self, reference: str) -> str: ...


class EnvironmentSecretProvider:
    def resolve(self, reference: str) -> str:
        if reference not in os.environ:
            raise KeyError(f"secret reference is unavailable: {reference}")
        return os.environ[reference]


class SandboxExecutor(Protocol):
    def execute(self, effect: EffectRequest, policy: SandboxPolicy) -> ExecutionResult: ...


class LocalTrustedExecutor:
    def __init__(self, *, runner=subprocess.run):
        self.runner = runner

    def execute(self, effect: EffectRequest, policy: SandboxPolicy) -> ExecutionResult:
        del policy
        if effect.secret_refs:
            raise PermissionError("trusted local executor does not inject secret references")
        if effect.network_hosts:
            raise PermissionError("trusted local executor cannot enforce an egress allowlist")
        workspace = effect.workspace.expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        result = self.runner(
            list(effect.command),
            cwd=str(workspace),
            env={**os.environ, **dict(effect.environment)},
            capture_output=True,
            text=True,
            timeout=effect.timeout_seconds,
            shell=False,
        )
        return ExecutionResult(
            effect.effect_id,
            int(result.returncode),
            str(result.stdout)[-20000:],
            str(result.stderr)[-20000:],
            time.monotonic() - started,
        )


class DockerExecutor:
    def __init__(
        self,
        *,
        secret_provider: SecretProvider | None = None,
        runner=subprocess.run,
        process_factory=subprocess.Popen,
        docker_binary: str = "docker",
    ):
        self.secret_provider = secret_provider or EnvironmentSecretProvider()
        self.runner = runner
        self.process_factory = process_factory
        self.docker_binary = docker_binary

    def execute(self, effect: EffectRequest, policy: SandboxPolicy) -> ExecutionResult:
        if policy.image not in set(policy.allowed_images):
            raise PermissionError(f"container image is not approved: {policy.image}")
        requested_hosts = set(effect.network_hosts)
        if not requested_hosts.issubset(set(policy.allow_network_hosts)):
            raise PermissionError("effect requests hosts outside the egress allowlist")
        if requested_hosts and not (policy.egress_network and policy.egress_proxy_url):
            raise PermissionError("networked effects require a controlled egress network and proxy")

        workspace = effect.workspace.expanduser().resolve()
        roots = tuple(root.expanduser().resolve() for root in policy.allowed_workspace_roots)
        if roots and not any(workspace.is_relative_to(root) for root in roots):
            raise PermissionError("effect workspace is outside approved writable roots")
        workspace.mkdir(parents=True, exist_ok=True)
        env_file: str | None = None
        secret_values: list[str] = []
        try:
            resolved_environment = dict(effect.environment)
            if requested_hosts:
                resolved_environment.update(
                    HTTP_PROXY=policy.egress_proxy_url or "",
                    HTTPS_PROXY=policy.egress_proxy_url or "",
                    NO_PROXY="localhost,127.0.0.1",
                    HARNESS_EGRESS_ALLOWLIST=",".join(sorted(requested_hosts)),
                )
            for name, reference in effect.secret_refs.items():
                value = self.secret_provider.resolve(reference)
                resolved_environment[name] = value
                secret_values.append(value)
            if resolved_environment:
                handle = tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    delete=False,
                    prefix="harness-env-",
                    suffix=".list",
                )
                env_file = handle.name
                try:
                    for name, value in resolved_environment.items():
                        if "\n" in value or "\r" in value:
                            raise ValueError("environment values may not contain newlines")
                        handle.write(f"{name}={value}\n")
                finally:
                    handle.close()
            command = self._docker_command(effect, policy, env_file)
            started = time.monotonic()
            if effect.cancel_check is None and effect.checkpoint_callback is None:
                try:
                    result = self.runner(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=effect.timeout_seconds + 15,
                        shell=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    self.runner(
                        [self.docker_binary, "rm", "-f", self._container_name(effect.effect_id)],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        shell=False,
                    )
                    result = _ProcessResult(
                        124,
                        str(exc.stdout or ""),
                        f"{exc.stderr or ''}\nactivity timed out",
                    )
            else:
                result = self._run_cancellable(
                    command,
                    effect,
                    self._container_name(effect.effect_id),
                )
            return ExecutionResult(
                effect.effect_id,
                int(result.returncode),
                _redact_values(str(result.stdout), secret_values)[-20000:],
                _redact_values(str(result.stderr), secret_values)[-20000:],
                time.monotonic() - started,
            )
        finally:
            if env_file:
                try:
                    os.unlink(env_file)
                except FileNotFoundError:
                    pass

    def _docker_command(
        self,
        effect: EffectRequest,
        policy: SandboxPolicy,
        env_file: str | None,
    ) -> list[str]:
        network = policy.egress_network if effect.network_hosts else "none"
        container_name = self._container_name(effect.effect_id)
        command = [
            self.docker_binary,
            "run",
            "--rm",
            "--name",
            container_name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            str(network),
            "--cpus",
            str(policy.cpu_limit),
            "--memory",
            policy.memory_limit,
            "--pids-limit",
            str(policy.pids_limit),
            "--ulimit",
            "nofile=1024:1024",
            "--stop-timeout",
            "5",
            "--mount",
            f"type=bind,src={effect.workspace.resolve()},dst=/workspace",
            "--workdir",
            "/workspace",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
        ]
        if env_file:
            command.extend(["--env-file", env_file])
        command.append(policy.image)
        command.extend(effect.command)
        return command

    def _run_cancellable(
        self,
        command: list[str],
        effect: EffectRequest,
        container_name: str,
    ):
        process = self.process_factory(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        deadline = time.monotonic() + effect.timeout_seconds
        cancelled = False
        timed_out = False
        checkpoint_error: Exception | None = None
        checkpoint_position = 0

        def drain_checkpoints() -> None:
            nonlocal checkpoint_position
            if not effect.checkpoint_path or not effect.checkpoint_callback:
                return
            path = effect.checkpoint_path
            if not path.exists():
                return
            with path.open("r", encoding="utf-8") as stream:
                stream.seek(checkpoint_position)
                for line in stream:
                    if line.strip():
                        effect.checkpoint_callback(json.loads(line))
                checkpoint_position = stream.tell()

        try:
            while process.poll() is None:
                drain_checkpoints()
                if effect.cancel_check and effect.cancel_check():
                    cancelled = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.1)
            drain_checkpoints()
        except Exception as exc:
            checkpoint_error = exc
            cancelled = True
        if cancelled or timed_out:
            self.runner(
                [self.docker_binary, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
            )
            try:
                process.terminate()
            except OSError:
                pass
        stdout, stderr = process.communicate(timeout=15)
        if checkpoint_error is not None:
            raise checkpoint_error
        if cancelled:
            return _ProcessResult(130, stdout, f"{stderr}\nactivity cancelled")
        if timed_out:
            return _ProcessResult(124, stdout, f"{stderr}\nactivity timed out")
        return _ProcessResult(int(process.returncode), stdout, stderr)

    @staticmethod
    def _container_name(effect_id: str) -> str:
        digest = hashlib.sha256(effect_id.encode("utf-8")).hexdigest()[:20]
        return f"harness-{digest}"


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _redact_values(value: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    return value
