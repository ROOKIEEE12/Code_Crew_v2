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


def _last_error(stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR")]
    return (errors[-1] if errors else (lines[-1] if lines else "pip failed"))[:300]


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


def _python_in(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


class LocalSandbox(Sandbox):
    name = "local-venv"
    strict = False

    def __init__(self, workspace: Path, venv_dir: Path | None = None,
                 project=None) -> None:
        super().__init__(workspace)
        self.venv_dir = venv_dir or default_venv_dir()
        self._module_cache: dict[str, bool] = {}
        #: A codecrew.project.Project; decides whether a project
        #: environment (the project's own dependencies) is needed.
        self.project = project
        #: Set when the project's dependencies live in their own venv.
        self.env_dir: Path | None = None
        #: False when the project declares dependencies that are not
        #: installed, so an ImportError in its tests is about the
        #: environment rather than the code.
        self.dependencies_ready = True
        #: One sentence for the report about the execution environment.
        self.environment_note = ""
        #: "src" for a src-layout project: put on PYTHONPATH so the tests
        #: import the code the way the project's own config says to.
        self._source_root = getattr(project, "source_root", "") or ""

    @property
    def python(self) -> Path:
        return _python_in(self.venv_dir)

    @property
    def run_python(self) -> Path:
        """The interpreter commands run with: the project env when there is one."""
        return _python_in(self.env_dir) if self.env_dir else self.python

    def for_workspace(self, workspace: Path) -> "LocalSandbox":
        """Same environment, different directory (a scratch copy, say)."""
        clone = LocalSandbox(workspace, self.venv_dir, self.project)
        clone.env_dir = self.env_dir
        clone.dependencies_ready = self.dependencies_ready
        clone.environment_note = self.environment_note
        clone._module_cache = dict(self._module_cache)
        clone._source_root = getattr(self, "_source_root", "")
        return clone

    @property
    def _stamp(self) -> Path:
        return self.venv_dir / ".codecrew-requirements"

    def _requirements_digest(self) -> str:
        joined = "\n".join(SANDBOX_REQUIREMENTS)
        return hashlib.sha256(joined.encode()).hexdigest()[:16]

    def ensure(self) -> None:
        self._ensure_tools()
        self._ensure_project_env()

    def _ensure_tools(self) -> None:
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

    # -- the project's own dependencies -------------------------------------------

    def _ensure_project_env(self) -> None:
        """Give a project with declared dependencies an environment that has them.

        One venv per distinct dependency set, under ~/.codecrew/envs, shared
        by every project that declares the same set and never written into
        the project itself. The gate tools are not reinstalled: a .pth file
        makes the tool venv's packages importable from it, after the
        project's own (so a project pinning its own pytest gets that one).
        """
        project = self.project
        if project is None or not project.has_dependencies():
            return
        if not project.should_install():
            self.dependencies_ready = False
            self.environment_note = (
                "The project declares dependencies that were NOT installed "
                "(a generated project; set install-dependencies = true in "
                "[tool.codecrew] to allow it).")
            return

        key = hashlib.sha256(
            f"{self._requirements_digest()}|{project.dependency_digest()}|"
            f"{sys.version_info[:2]}".encode()).hexdigest()[:16]
        env_dir = self.venv_dir.parent / "envs" / key
        stamp = env_dir / ".codecrew-ready"
        if stamp.exists() and _python_in(env_dir).exists():
            self._use_env(env_dir, cached=True)
            return

        log = self.venv_dir.parent / "envs" / f"{key}.install.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not _python_in(env_dir).exists():
                # Built from the tool venv's interpreter, so the Python
                # version (and so the .pth'd tool packages) always match.
                made = subprocess.run(
                    [str(self.python), "-m", "venv", str(env_dir)],
                    capture_output=True, text=True, timeout=300)
                if made.returncode != 0:
                    raise SandboxError(made.stderr[-600:])
            tool_site = self._purelib(self.python)
            env_site = self._purelib(_python_in(env_dir))
            Path(env_site, "_codecrew_tools.pth").write_text(tool_site + "\n",
                                                             encoding="utf-8")
            install = subprocess.run(
                [str(_python_in(env_dir)), "-m", "pip", "install",
                 "--disable-pip-version-check", "--quiet", *project.install_args()],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=1200, cwd=str(project.root))
            log.write_text(install.stdout + "\n" + install.stderr, encoding="utf-8")
            if install.returncode != 0:
                raise SandboxError(_last_error(install.stderr))
        except (OSError, subprocess.SubprocessError, SandboxError) as exc:
            self.dependencies_ready = False
            self.environment_note = (
                f"The project's dependencies could not be installed ({exc}). "
                f"Tests that import them cannot run; see {log}.")
            return
        stamp.write_text(project.dependency_digest(), encoding="utf-8")
        self._use_env(env_dir, cached=False)

    def _use_env(self, env_dir: Path, *, cached: bool) -> None:
        self.env_dir = env_dir
        self.dependencies_ready = True
        self._module_cache.clear()
        count = len(self.project.dependencies) + len(self.project.requirement_files)
        self.environment_note = (
            f"Project dependencies installed in an isolated environment "
            f"({'cached' if cached else 'created'}: {env_dir}); "
            f"{count} declaration(s).")

    def _purelib(self, python: Path) -> str:
        done = subprocess.run(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            capture_output=True, text=True, timeout=60)
        if done.returncode != 0 or not done.stdout.strip():
            raise SandboxError(f"cannot locate site-packages for {python}")
        return done.stdout.strip()

    def _resolve(self, args: list[str]) -> list[str]:
        if args and args[0] == "python":
            return [str(self.run_python), *args[1:]]
        return list(args)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Keep host site-packages and user config out of the run so results
        # are reproducible on a different machine.
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONPATH", None)
        if self._source_root:
            env["PYTHONPATH"] = str(self.workspace / self._source_root)
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
