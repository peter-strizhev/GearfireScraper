import json
import io
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from deploy.dashboard import Dashboard, _run_logged, make_handler


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dashboard = Dashboard({
            "output_dir": self.tmp.name,
            "site_url": "https://example.test",
            "delay": 0.5,
            "rules": "/rules.csv",
        })
        self.dashboard.sections = ["rifles/semi-automatic", "rifles/semi-automatic-parts"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.dashboard))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, *, body=None, token=None):
        headers = {}
        if token is not None:
            headers["X-Dashboard-Token"] = token
        data = json.dumps(body).encode() if body is not None else None
        return urllib.request.urlopen(urllib.request.Request(self.url + path, data=data, headers=headers))

    def test_selected_section_scan_and_download(self):
        def fake_crawl(command, log):
            output = Path(command[command.index("--out-dir") + 1])
            self.assertEqual(command[command.index("--category") + 1], "rifles/semi-automatic")
            self.assertIn("--exact-category", command)
            (output / "inventory.csv").write_text("UPC\n123\n")
            (output / "wa-restricted-items.csv").write_text("UPC\n123\n")
            (output / "progress.json").write_text(json.dumps({
                "status": "complete", "current_section": "rifles/semi-automatic",
                "current_item": "Example product (ID: 123)",
                "sections_started": ["rifles/semi-automatic"],
            }))
            return 0

        with patch("deploy.dashboard._run_logged", side_effect=fake_crawl):
            with self.request("/api/scan", body={"section": "rifles/semi-automatic"},
                              token=self.dashboard.token) as response:
                self.assertEqual(response.status, 202)
            for _ in range(100):
                if self.dashboard.status()["status"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(self.dashboard.status()["status"], "complete")
            with self.request("/api/status") as response:
                status = json.load(response)
            self.assertEqual(status["progress"]["manual"]["current_section"], "rifles/semi-automatic")
            self.assertEqual(status["progress"]["manual"]["current_item"], "Example product (ID: 123)")
            with self.request("/download/report") as response:
                self.assertEqual(response.read(), b"UPC\n123\n")
                self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(Dashboard(self.dashboard.config).status()["status"], "complete")

    def test_invalid_section_and_token_are_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/scan", body={"section": ""})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/scan", body={"section": "not-a-section"}, token=self.dashboard.token)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_all_sections_and_failure(self):
        def fake_crawl(command, log):
            self.assertNotIn("--category", command)
            return 1

        with patch("deploy.dashboard._run_logged", side_effect=fake_crawl):
            self.dashboard.start("")
            for _ in range(100):
                if self.dashboard.status()["status"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(self.dashboard.status()["status"], "failed")
            self.assertIsNone(self.dashboard.file("report"))
            self.assertIsNotNone(self.dashboard.file("log"))

    def test_only_one_dashboard_scan_at_a_time(self):
        with self.dashboard.lock:
            self.dashboard.state = {"status": "running"}
        with self.assertRaisesRegex(RuntimeError, "already running"):
            self.dashboard.start("")

    def test_scheduled_progress_is_shown_and_invalid_progress_reported(self):
        progress = Path(self.tmp.name) / "progress.json"
        progress.write_text(json.dumps({
            "status": "running", "current_section": "rifles/one",
            "sections_started": ["rifles/one"],
        }))
        with self.request("/api/status") as response:
            status = json.load(response)
        self.assertEqual(status["progress"]["scheduled"]["current_section"], "rifles/one")
        progress.write_text("invalid json")
        self.assertIn("Could not read", self.dashboard.status()["progress"]["scheduled"]["error"])

    def test_sections_persist_and_failed_refresh_preserves_them(self):
        result = subprocess.CompletedProcess([], 0, stdout='["rifles/one", "rifles/two"]', stderr="")
        with patch("deploy.dashboard.subprocess.run", return_value=result) as fetch:
            self.assertEqual(self.dashboard.load_sections(), ["rifles/one", "rifles/two"])
            fetch.assert_called_once()
        restarted = Dashboard(self.dashboard.config)
        with patch("deploy.dashboard.subprocess.run") as fetch:
            self.assertEqual(restarted.load_sections(), ["rifles/one", "rifles/two"])
            fetch.assert_not_called()
        failure = subprocess.CompletedProcess([], 1, stdout="", stderr="store unavailable")
        with patch("deploy.dashboard.subprocess.run", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "store unavailable"):
                restarted.load_sections(refresh=True)
        self.assertEqual(Dashboard(self.dashboard.config).load_sections(), ["rifles/one", "rifles/two"])

    def test_sections_do_not_carry_over_to_a_different_store(self):
        self.dashboard.sections_path.write_text(json.dumps({
            "site_url": "https://other.test", "sections": ["other/section"],
        }))
        restarted = Dashboard(self.dashboard.config)
        result = subprocess.CompletedProcess([], 0, stdout='["rifles/one"]', stderr="")
        with patch("deploy.dashboard.subprocess.run", return_value=result) as fetch:
            self.assertEqual(restarted.load_sections(), ["rifles/one"])
            fetch.assert_called_once()

    def test_refresh_endpoint_requires_token_and_replaces_sections(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/sections/refresh", body={})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        result = subprocess.CompletedProcess([], 0, stdout='["new/section"]', stderr="")
        with patch("deploy.dashboard.subprocess.run", return_value=result):
            with self.request("/api/sections/refresh", body={}, token=self.dashboard.token) as response:
                self.assertEqual(json.load(response), ["new/section"])
        self.assertEqual(Dashboard(self.dashboard.config).load_sections(), ["new/section"])

    def test_crawler_output_is_streamed_to_log_and_container_output(self):
        seen = threading.Event()

        class SignallingLog(io.StringIO):
            def write(self, text):
                result = super().write(text)
                seen.set()
                return result

        log, console = SignallingLog(), io.StringIO()
        command = [sys.executable, "-u", "-c",
                   "import time; print('Scanning section: rifles/one'); time.sleep(1)"]
        with redirect_stdout(console):
            thread = threading.Thread(target=_run_logged, args=(command, log))
            thread.start()
            self.assertTrue(seen.wait(5))
            self.assertTrue(thread.is_alive())
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIn("Scanning section: rifles/one", log.getvalue())
        self.assertIn("[dashboard scan] Scanning section: rifles/one", console.getvalue())


if __name__ == "__main__":
    unittest.main()
