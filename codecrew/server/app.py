"""The local HTTP API, on the standard library only.

The server runs builds, which means it runs generated code. Anything that
can reach it can do the same, so it is locked to the person at the machine:

* it binds to loopback by default;
* it rejects any ``Host`` that is not a loopback name, which defeats DNS
  rebinding (a web page pointing its own domain at 127.0.0.1);
* it rejects any browser ``Origin`` that is not the UI itself, so another
  site open in the same browser cannot drive it;
* writes must be ``application/json``, which a cross-site form cannot send
  without a CORS preflight that the origin check then refuses;
* paths are confined to the workspace roots given at start-up.
"""

from __future__ import annotations

import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from ..ci_export import DEFAULT_SOURCE, render_workflow
from ..llm import provider_status
from ..sandbox import docker_available
from .jobs import JobError, JobManager, available_replays

MAX_BODY = 1_000_000
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEV_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")

_JOB = re.compile(r"^/api/jobs/([a-f0-9]{12})(/approval|/cancel|/ci)?$")


class CodeCrewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager: JobManager, *, ui_dir: Path | None,
                 origins: set[str]) -> None:
        super().__init__(address, Handler)
        self.manager = manager
        self.ui_dir = ui_dir.resolve() if ui_dir else None
        port = self.server_address[1]
        self.origins = set(origins) | {f"http://127.0.0.1:{port}",
                                       f"http://localhost:{port}"}


class Handler(BaseHTTPRequestHandler):
    server: CodeCrewServer
    server_version = "codecrew"
    sys_version = ""

    # -- plumbing ----------------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return  # quiet; the UI shows progress, the traces keep the record

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in self.server.origins

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):
            name = host[1:].split("]", 1)[0]
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        bound = self.server.server_address[0]
        return name.lower() in LOOPBACK_HOSTS or name == bound

    def _guard(self) -> bool:
        if not self._host_ok():
            self._json({"error": "unexpected Host header"}, HTTPStatus.FORBIDDEN)
            return False
        if not self._origin_ok():
            self._json({"error": "origin not allowed"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin in self.server.origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _json(self, payload, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict | None:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            self._json({"error": "Content-Type must be application/json"},
                       HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json({"error": "body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "body is not valid JSON"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(data, dict):
            self._json({"error": "body must be a JSON object"}, HTTPStatus.BAD_REQUEST)
            return None
        return data

    # -- verbs ------------------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._guard():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/api/health":
                return self._json(self._health())
            if url.path == "/api/jobs":
                return self._json({"jobs": self.server.manager.list_jobs()})
            if url.path == "/api/workspaces":
                return self._json({"workspaces": self.server.manager.workspaces()})
            if url.path == "/api/workspace":
                raw = (query.get("path") or [""])[0]
                return self._json(self.server.manager.workspace_detail(raw))
            if url.path == "/api/evaluations":
                from ..bench.ablation import list_evaluations
                eval_dir = self.server.manager.eval_dir
                return self._json({"evaluations": list_evaluations(eval_dir)
                                   if eval_dir else []})
            match = _JOB.match(url.path)
            if match and match.group(2) is None:
                since = _int((query.get("since") or ["0"])[0])
                return self._json(self.server.manager.get(match.group(1)).detail(since))
            if match and match.group(2) == "/ci":
                job = self.server.manager.get(match.group(1))
                return self._json({"workflow": render_workflow(
                    source=DEFAULT_SOURCE), "workspace": job.workspace})
            if url.path.startswith("/api/"):
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return self._static(url.path)
        except JobError as exc:
            return self._json({"error": str(exc)}, exc.status)

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        body = self._body()
        if body is None:
            return
        url = urlsplit(self.path)
        manager = self.server.manager
        try:
            if url.path == "/api/jobs":
                job = manager.submit(body)
                return self._json(job.detail(), HTTPStatus.CREATED)
            match = _JOB.match(url.path)
            if match and match.group(2) == "/approval":
                job = manager.get(match.group(1))
                approve = body.get("approve")
                if not isinstance(approve, bool):
                    raise JobError("'approve' must be true or false")
                edited = body.get("edited")
                if edited is not None and not isinstance(edited, dict):
                    raise JobError("'edited' must be an object")
                job.decide(approve, edited)
                return self._json({"ok": True})
            if match and match.group(2) == "/cancel":
                return self._json(manager.cancel(match.group(1)).summary())
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except JobError as exc:
            return self._json({"error": str(exc)}, exc.status)

    # -- helpers ----------------------------------------------------------------------

    def _health(self) -> dict:
        providers = [{"name": name, "ready": ok, "reason": reason}
                     for name, ok, reason in provider_status()]
        providers.append({"name": "scripted", "ready": True,
                          "reason": "offline replay of a bundled script"})
        manager = self.server.manager
        return {
            "version": __version__,
            "providers": providers,
            "replays": available_replays(manager.replays_dir),
            "sandbox": {"docker": docker_available()},
            "roots": [str(r) for r in manager.roots],
        }

    def _static(self, raw_path: str) -> None:
        root = self.server.ui_dir
        if root is None:
            return self._json({"error": "the web UI is not built; run "
                                        "`npm run build` in web/, or use "
                                        "`npm run dev` there"},
                              HTTPStatus.NOT_FOUND)
        relative = raw_path.lstrip("/")
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        candidates = [target, target / "index.html", target.with_suffix(".html")]
        found = next((c for c in candidates if c.is_file()), None)
        status = HTTPStatus.OK
        if found is None:
            found, status = root / "404.html", HTTPStatus.NOT_FOUND
            if not found.is_file():
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        body = found.read_bytes()
        ctype = mimetypes.guess_type(found.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)


def _int(raw: str) -> int:
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    manager: JobManager,
    ui_dir: Path | None = None,
    extra_origins: list[str] | tuple[str, ...] = (),
) -> CodeCrewServer:
    return CodeCrewServer((host, port), manager, ui_dir=ui_dir,
                          origins={*DEV_ORIGINS, *extra_origins})
