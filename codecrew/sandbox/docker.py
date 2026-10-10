"""Container backend: real isolation for generated code.

The tools are baked into an image once (needs network), after which every
command runs with ``--network none`` and capped resources.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..config import SANDBOX_REQUIREMENTS
from .base import ExecResult, Sandbox, SandboxError

_DOCKERFILE = """\
FROM python:3.10-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir {requirements}
RUN useradd --create-home --uid 1000 runner
WORKDIR /work
USER runner
"""


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True,
    )
    return probe.returncode == 0


class DockerSandbox(Sandbox):
    name = "docker"
    strict = True

    #: Resource caps applied to every command.
    memory = "1g"
    cpus = "1.0"
    pids = "256"

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self._module_cache: dict[str, bool] = {}

    @property
    def image_tag(self) -> str:
        digest = hashlib.sha256("\n".join(SANDBOX_REQUIREMENTS).encode()).hexdigest()[:12]
        return f"codecrew-sandbox:{digest}"

    def _image_exists(self) -> bool:
        probe = subprocess.run(
            ["docker", "image", "inspect", self.image_tag],
            capture_output=True, text=True,
        )
        return probe.returncode == 0

    def ensure(self) -> None:
        if self._image_exists():
            return
        dockerfile = _DOCKERFILE.format(
            requirements=" ".join(f'"{r}"' for r in SANDBOX_REQUIREMENTS)
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            build = subprocess.run(
                ["docker", "build", "-t", self.image_tag, tmp],
                capture_output=True, text=True,
            )
        if build.returncode != 0:
            raise SandboxError(
                "sandbox image build failed (network needed once):\n"
                + (build.stderr or "")[-1500:]
            )

    def _resolve(self, args: list[str]) -> list[str]:
        if args and args[0] == "python":
            return ["python", *args[1:]]
        return list(args)

    def run(self, args: list[str], *, timeout: int, cwd: str | None = None) -> ExecResult:
        workdir = f"/work/{cwd}" if cwd else "/work"
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", self.pids,
            # The workspace is writable (tests produce coverage data) but the
            # rest of the container filesystem is not.
            "--read-only",
            "--tmpfs", "/tmp:rw,size=256m",
            "-v", f"{self.workspace}:/work:rw",
            "-w", workdir,
            self.image_tag,
            *self._resolve(args),
        ]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                docker_cmd,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout + 30,  # allow for container startup
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                command=docker_cmd, returncode=124, stdout="", stderr="",
                duration_s=time.monotonic() - started, timed_out=True,
            )
        return ExecResult(
            command=docker_cmd,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_s=time.monotonic() - started,
        )

    def has_module(self, module: str) -> bool:
        if module not in self._module_cache:
            result = self.run(
                ["python", "-c", f"import importlib.util as u,sys;"
                                 f"sys.exit(0 if u.find_spec('{module}') else 1)"],
                timeout=120,
            )
            self._module_cache[module] = result.ok
        return self._module_cache[module]
