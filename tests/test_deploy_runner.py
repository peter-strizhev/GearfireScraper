import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from deploy.runner import _read_config, _write_config, next_run


class DeploymentRunnerTests(unittest.TestCase):
    def test_next_run_defaults_to_the_following_sunday_after_noon(self):
        now = datetime.fromisoformat("2026-11-15T12:00:01-08:00")
        result = next_run(
            now,
            day="sunday",
            run_time="12:00",
            timezone="America/Los_Angeles",
        )
        self.assertEqual(result.isoformat(), "2026-11-22T12:00:00-08:00")

    def test_next_run_uses_timezone_and_dst(self):
        now = datetime.fromisoformat("2026-03-07T10:00:00-08:00")
        result = next_run(
            now,
            day="sunday",
            run_time="12:00",
            timezone="America/Los_Angeles",
        )
        self.assertEqual(result.isoformat(), "2026-03-08T12:00:00-07:00")

    def test_schedule_is_persisted_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            config = {
                "day": "sunday",
                "time": "12:00",
                "timezone": "America/Los_Angeles",
                "site_url": "https://example.test",
                "rules": "/app/washington-rules.csv",
                "output_dir": "/data/output",
                "delay": 0.5,
            }
            _write_config(path, config)
            self.assertEqual(_read_config(path), config)
            self.assertEqual(json.loads(path.read_text())["output_dir"], "/data/output")


if __name__ == "__main__":
    unittest.main()
