"""Local API and UI host: ``codecrew serve``."""

from __future__ import annotations

import sys
from pathlib import Path

from .app import CodeCrewServer, create_server
from .jobs import Job, JobError, JobManager

__all__ = ["CodeCrewServer", "Job", "JobError", "JobManager", "create_server",
           "serve", "default_ui_dir", "default_replays_dir"]

_REPO = Path(__file__).resolve().parents[2]


def default_ui_dir() -> Path | None:
    """``web/out`` from a source checkout, if the UI has been built."""
    candidate = _REPO / "web" / "out"
    return candidate if (candidate / "index.html").is_file() else None


def default_replays_dir() -> Path | None:
    candidate = _REPO / "samples" / "replays"
    return candidate if candidate.is_dir() else None


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    roots: list[Path] | None = None,
    ui_dir: Path | None = None,
    extra_origins: list[str] | None = None,
) -> int:
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: binding to {host} exposes a server that runs "
              f"generated code. Requests still need a loopback Host header.",
              file=sys.stderr)
    roots = roots or [Path.home() / "codecrew-workspaces"]
    manager = JobManager(roots, replays_dir=default_replays_dir(),
                         eval_dir=_REPO / "eval_runs")
    ui = ui_dir or default_ui_dir()
    server = create_server(host=host, port=port, manager=manager, ui_dir=ui,
                           extra_origins=extra_origins or [])
    url = f"http://{'127.0.0.1' if host in ('127.0.0.1', '::1') else host}:{server.server_address[1]}"
    print(f"CodeCrew API on {url}/api")
    if ui:
        print(f"Web UI        on {url}/")
    else:
        print("Web UI not built. For development: cd web && npm run dev "
              "(then open http://localhost:3000)")
    print(f"Workspaces in : {', '.join(str(r) for r in manager.roots)}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.shutdown()
    return 0
