"""Local web dashboard for manually running and downloading crawler reports."""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from deploy.runner import _crawl_command, _write_config

HTML = Path(__file__).with_name("dashboard.html")
SECTIONS_SCRIPT = Path(__file__).with_name("sections.py")


def _run_logged(command: list[str], log: TextIO) -> int:
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", bufsize=1) as process:
        if process.stdout is None:
            raise RuntimeError("Could not capture crawler output")
        for line in process.stdout:
            log.write(line)
            log.flush()
            print(f"[dashboard scan] {line}", end="", flush=True)
        return process.wait()


def _read_progress(path: Path) -> dict[str, object] | None:
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        return {"error": f"Could not read scan progress: {error}"}
    if (not isinstance(progress, dict) or not isinstance(progress.get("status"), str)
            or not isinstance(progress.get("current_section"), str)
            or not isinstance(progress.get("sections_started"), list)
            or not all(isinstance(item, str) for item in progress["sections_started"])):
        return {"error": f"Invalid scan progress in {path}"}
    return progress


class Dashboard:
    def __init__(self, config: dict[str, object]):
        self.config = config
        self.root = Path(str(config["output_dir"])) / "dashboard"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "latest.json"
        self.sections_path = self.root / "sections.json"
        self.lock = threading.Lock()
        self.sections_lock = threading.Lock()
        self.sections: list[str] = []
        self.sections_loaded = False
        self.sections_error = ""
        self.token = secrets.token_urlsafe(32)
        self.state: dict[str, str] = {"status": "idle"}
        try:
            saved_sections = json.loads(self.sections_path.read_text(encoding="utf-8"))
            if (not isinstance(saved_sections, dict) or not isinstance(saved_sections.get("site_url"), str)
                    or not isinstance(saved_sections.get("sections"), list)
                    or not all(isinstance(s, str) and s for s in saved_sections["sections"])):
                raise ValueError("Invalid saved section list")
            if saved_sections["site_url"] == self.config["site_url"]:
                self.sections = saved_sections["sections"]
                self.sections_loaded = True
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            self.sections_error = f"Could not load saved sections: {error}. Refresh the list."
        if self.state_path.exists():
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("id"), str):
                raise ValueError(f"Invalid dashboard state: {self.state_path}")
            self.state = saved
            if self.state.get("status") == "running":
                self.state["status"] = "interrupted"
                self.state["error"] = "The dashboard restarted before this scan finished."
                _write_config(self.state_path, self.state)

    def load_sections(self, *, refresh: bool = False) -> list[str]:
        with self.sections_lock:
            if not refresh and self.sections_loaded:
                with self.lock:
                    return list(self.sections)
            if not refresh and self.sections_error:
                raise RuntimeError(self.sections_error)
            return self._refresh_sections()

    def _refresh_sections(self) -> list[str]:
        result = subprocess.run(
            [sys.executable, str(SECTIONS_SCRIPT), str(self.config["site_url"]),
             str(self.config["delay"])],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Could not load sections: {result.stderr.strip() or 'storefront request failed'}")
        try:
            sections = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("The storefront returned an invalid section list") from error
        if not isinstance(sections, list) or not all(isinstance(s, str) and s for s in sections):
            raise RuntimeError("The storefront returned an invalid section list")
        _write_config(self.sections_path, {"site_url": self.config["site_url"], "sections": sections})
        with self.lock:
            self.sections = sections
            self.sections_loaded = True
            self.sections_error = ""
        return sections

    def start(self, section: str) -> None:
        with self.lock:
            if self.state.get("status") == "running":
                raise RuntimeError("A dashboard scan is already running")
            if section and section not in self.sections:
                raise ValueError("Select a section from the current list")
            scan_id = uuid.uuid4().hex
            output = self.root / scan_id
            output.mkdir()
            self.state = {"id": scan_id, "status": "running", "section": section}
            _write_config(self.state_path, self.state)
            thread = threading.Thread(target=self._scan, args=(output, section), daemon=True)
            thread.start()

    def _scan(self, output: Path, section: str) -> None:
        config = dict(self.config)
        config.update(output_dir=str(output), categories=[section] if section else [],
                      exact_categories=True, max_products=None)
        error = ""
        try:
            with (output / "crawl.log").open("w", encoding="utf-8") as log:
                returncode = _run_logged(_crawl_command(config), log)
            if returncode:
                error = f"Crawl failed with exit code {returncode}. See the scan log."
            elif not all((output / name).is_file() for name in ("inventory.csv", "wa-restricted-items.csv")):
                error = "Crawl finished without producing both CSV files."
        except (OSError, RuntimeError) as exc:
            error = f"Could not run crawl: {exc}"
        with self.lock:
            self.state["status"] = "failed" if error else "complete"
            if error:
                self.state["error"] = error
            _write_config(self.state_path, self.state)

    def status(self) -> dict[str, object]:
        with self.lock:
            state: dict[str, object] = dict(self.state)
            scan_id = self.state.get("id", "")
        manual = None
        if scan_id and all(c in "0123456789abcdef" for c in scan_id):
            manual = _read_progress(self.root / scan_id / "progress.json")
        state["progress"] = {
            "manual": manual,
            "scheduled": _read_progress(Path(str(self.config["output_dir"])) / "progress.json"),
        }
        return state

    def file(self, kind: str) -> Path | None:
        names = {"inventory": "inventory.csv", "report": "wa-restricted-items.csv", "log": "crawl.log"}
        with self.lock:
            scan_id = self.state.get("id", "")
            if kind not in names or not scan_id or not all(c in "0123456789abcdef" for c in scan_id):
                return None
            if kind != "log" and self.state.get("status") != "complete":
                return None
            path = self.root / scan_id / names[kind]
            return path if path.is_file() else None


def make_handler(dashboard: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, status: int, content: bytes, content_type: str, filename: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, status: int, value: object) -> None:
            self.send_bytes(status, json.dumps(value).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                page = HTML.read_text(encoding="utf-8").replace("__TOKEN__", dashboard.token)
                self.send_bytes(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self.send_json(HTTPStatus.OK, dashboard.status())
            elif path == "/api/sections":
                try:
                    self.send_json(HTTPStatus.OK, dashboard.load_sections())
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            elif path.startswith("/download/"):
                file = dashboard.file(path.removeprefix("/download/"))
                if file is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "File not available"})
                    return
                try:
                    self.send_bytes(HTTPStatus.OK, file.read_bytes(), "text/plain; charset=utf-8" if file.suffix == ".log" else "text/csv; charset=utf-8", file.name)
                except OSError as error:
                    self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:
            if self.path not in ("/api/scan", "/api/sections/refresh"):
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            if not secrets.compare_digest(self.headers.get("X-Dashboard-Token", ""), dashboard.token):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid dashboard token"})
                return
            if self.path == "/api/sections/refresh":
                try:
                    self.send_json(HTTPStatus.OK, dashboard.load_sections(refresh=True))
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 4096:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get("section"), str):
                    raise ValueError("A section must be selected")
                dashboard.start(body["section"])
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            except RuntimeError as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            except OSError as error:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
                return
            self.send_json(HTTPStatus.ACCEPTED, dashboard.status())

    return Handler


def serve(config: dict[str, object], host: str, port: int) -> None:
    dashboard = Dashboard(config)
    server = ThreadingHTTPServer((host, port), make_handler(dashboard))
    print(f"Dashboard listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
