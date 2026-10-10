"""Local virtualenv backend.

Weaker than the container backend: it isolates *dependencies*, not the host.
Used when Docker is unavailable, and it always says so in the report.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

from ..config import SANDBOX_REQUIREMENTS
from .base import ExecResult, Sandbox, SandboxError


def default_venv_dir() -> Path:
    """Shared tool environment, outside any workspace.

    One environment serves every workspace: the tools are fixed and pinned,
    so rebuilding them per project would only cost time. Generated code
    still never installs into it - gates only ever read from it.
    """
    override = os.environ.get("CODECREW_VENV")
    if override:
        return Path(override)
    return Path.home() / ".codecrew" / "venv"


class LocalSandbox(Sandbox):
    name = "local-venv"
    strict = False

    def __init__(self, workspace: Path, venv_dir: Path | None = None) -> None:
        super().__init__(workspace)
        self.venv_dir = venv_dir or default_venv_dir()
        self._module_cache: dict[str, bool] = {}

    @property
    def python(self) -> Path:
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"

    @property
    def _stamp(self) -> Path:
        return self.venv_dir / ".codecrew-requirements"

    def _requirements_digest(self) -> str:
        joined = "\n".join(SANDBOX_REQUIREMENTS)
        return hashlib.sha256(joined.encode()).hexdigest()[:16]

    def ensure(self) -> None:
        digest = self._requirements_digest()
        if self.python.exists() and self._stamp.exists():
            if self._stamp.read_text(encoding="utf-8").strip() == digest:
                return

        if not self.python.exists():
            self.venv_dir.parent.mkdir(parents=True, exist_ok=True)
            venv.EnvBuilder(with_pip=True, clear=False).create(self.venv_dir)
        if not self.python.exists():
            raise SandboxError(f"virtualenv creation failed at {self.venv_dir}")

        install = subprocess.run(
            [
                str(self.python), "-m", "pip", "install",
                "--disable-pip-version-check", "--quiet",
                *SANDBOX_REQUIREMENTS,
            ],
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            # Offline is a normal state for this project, so do not hard-fail:
            # the individual gates detect their missing tool and report SKIP,
            # which is visible in the report rather than silently passing.
            sys.stderr.write(
                "[codecrew] warning: sandbox tool install failed; gates whose "
                "tools are missing will be reported as SKIP.\n"
                + (install.stderr or "")[-800:]
                + "\n"
            )
            return
        self._stamp.write_text(digest, encoding="utf-8")
        self._module_cache.clear()

    def _resolve(self, args: list[str]) -> list[str]:
        if args and args[0] == "python":
            return [str(self.python), *args[1:]]
        return list(args)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Keep host site-packages and user config out of the run so results
        # are reproducible on a different machine.
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONPATH", None)
        return env

    def run(self, args: list[str], *, timeout: int, cwd: str | None = None) -> ExecResult:
        resolved = self._resolve(args)
        workdir = self.workspace / cwd if cwd else self.workspace
        started = time.monotonic()
        try:
            proc = subprocess.run(
                resolved,
                cwd=str(workdir),
                env=self._env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                command=resolved,
                returncode=124,
                stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
                stderr=exc.stderr or "" if isinstance(exc.stderr, str) else "",
                duration_s=time.monotonic() - started,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            return ExecResult(
                command=resolved,
                returncode=127,
                stdout="",
                stderr=str(exc),
                duration_s=time.monotonic() - started,
            )
        return ExecResult(
            command=resolved,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_s=time.monotonic() - started,
        )

    def has_module(self, module: str) -> bool:
        if module in self._module_cache:
            return self._module_cache[module]
        result = self.run(
            ["python", "-c", f"import importlib.util as u,sys;"
                             f"sys.exit(0 if u.find_spec('{module}') else 1)"],
            timeout=60,
        )
        self._module_cache[module] = result.ok
        return result.ok
