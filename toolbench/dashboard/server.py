"""
HTTP server for the campaign dashboard (standard library only).

Routes (all GET, all read-only):

  /                     the dashboard page
  /static/<file>        its stylesheet and script
  /api/state            `CampaignReader.snapshot()` as JSON
  /api/summary?run=ID   a run's summary.txt as text/plain
  /api/trial?run=ID&trial=TID
                        a trial's row and console.log tail as JSON
  /api/prompts?run=ID&trial=TID
                        the trial's recorded system and user prompts
  /api/files?run=ID&trial=TID&path=DIR
                        one directory level of the trial's workspace, each
                        entry marked against its initial snapshot
  /api/file?run=ID&trial=TID&path=FILE
                        one workspace file's text (capped)

The workspace routes are only called while the page's sandbox view is on.

Binds to 127.0.0.1 by default; on a remote host, reach it through an SSH
port forward rather than binding a public interface.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from toolbench.dashboard.state import CampaignReader

STATIC_DIR = Path(__file__).parent / "static"
_TRIAL_ROUTES = ("/api/trial", "/api/prompts", "/api/files", "/api/file")
_CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                  ".css": "text/css; charset=utf-8",
                  ".js": "text/javascript; charset=utf-8"}


def make_handler(reader: CampaignReader, poll_s: float) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to one campaign reader."""

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "toolbench-dashboard"

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    self._send_static("index.html")
                elif url.path.startswith("/static/"):
                    self._send_static(url.path.removeprefix("/static/"))
                elif url.path == "/api/state":
                    state = reader.snapshot()
                    state["poll_s"] = poll_s
                    self._send_json(state)
                elif url.path == "/api/summary":
                    text = reader.run_summary(query.get("run", ""))
                    if text is None:
                        self.send_error(HTTPStatus.NOT_FOUND, "no summary.txt for this run yet")
                    else:
                        self._send(text.encode(), "text/plain; charset=utf-8")
                elif url.path in _TRIAL_ROUTES:
                    run, trial = query.get("run", ""), query.get("trial", "")
                    result = {
                        "/api/trial": lambda: reader.trial_detail(run, trial),
                        "/api/prompts": lambda: reader.trial_prompts(run, trial),
                        "/api/files": lambda: reader.list_files(run, trial, query.get("path", "")),
                        "/api/file": lambda: reader.read_file(run, trial, query.get("path", "")),
                    }[url.path]()
                    if result is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                    else:
                        self._send_json(result)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser navigated away mid-response

        def _send_static(self, name: str) -> None:
            path = (STATIC_DIR / name).resolve()
            if not path.is_relative_to(STATIC_DIR.resolve()) or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send(path.read_bytes(),
                       _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

        def _send_json(self, obj: object) -> None:
            self._send(json.dumps(obj, default=str).encode(), "application/json")

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            pass  # polling would flood the terminal

    return DashboardHandler


def serve(root: str | Path, *, host: str = "127.0.0.1", port: int = 8765,
          poll_s: float = 3.0) -> None:
    """Serve the dashboard for the campaign at `root` until interrupted."""
    reader = CampaignReader(root)
    httpd = ThreadingHTTPServer((host, port), make_handler(reader, poll_s))
    print(f"toolbench dashboard: {reader.root}")
    print(f"  http://{host}:{httpd.server_address[1]}/   (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
