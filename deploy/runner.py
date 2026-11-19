#!/usr/bin/env python3
"""Persist and run a weekly Gearfire crawl schedule."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_SCHEDULE_FILE = Path("/data/schedule.json")
DEFAULT_OUTPUT_DIR = Path("/data/output")
DEFAULT_TIMEZONE = "America/Los_Angeles"
DAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)


def _parse_time(value: str) -> datetime_time:
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as error:
        raise ValueError("time must use 24-hour HH:MM format") from error
    return parsed


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown IANA timezone: {name}") from error


def next_run(
    now: datetime,
    *,
    day: str,
    run_time: str,
    timezone: str,
) -> datetime:
    """Return the next scheduled instant, including the configured timezone."""
    day_name = day.casefold()
    if day_name not in DAY_NAMES:
        raise ValueError(f"day must be one of: {', '.join(DAY_NAMES)}")
    zone = _timezone(timezone)
    target_time = _parse_time(run_time)
    local_now = now.astimezone(zone)
    days_ahead = (DAY_NAMES.index(day_name) - local_now.weekday()) % 7
    candidate_date = local_now.date() + timedelta(days=days_ahead)
    candidate = datetime.combine(candidate_date, target_time, tzinfo=zone)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate


def _write_config(path: Path, config: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_config(path: Path) -> dict[str, object]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(
            f"schedule is not configured; run `configure --schedule-file {path}` first"
        ) from error
    except json.JSONDecodeError as error:
        raise ValueError(f"schedule file is not valid JSON: {path}") from error
    if not isinstance(config, dict):
        raise ValueError(f"schedule file must contain a JSON object: {path}")
    required = ("day", "time", "timezone", "site_url", "rules", "output_dir", "delay")
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(
            f"schedule file is missing required settings: {', '.join(missing)}"
        )
    next_run(
        datetime.now().astimezone(),
        day=str(config["day"]),
        run_time=str(config["time"]),
        timezone=str(config["timezone"]),
    )
    if not isinstance(config["site_url"], str) or not config["site_url"]:
        raise ValueError("schedule site_url must be a non-empty string")
    if not isinstance(config["rules"], str) or not config["rules"]:
        raise ValueError("schedule rules must be a non-empty path")
    if not isinstance(config["output_dir"], str) or not config["output_dir"]:
        raise ValueError("schedule output_dir must be a non-empty path")
    return config


def _crawl_command(config: dict[str, object]) -> list[str]:
    crawler = Path(__file__).resolve().parents[1] / "crawler" / "crawl.py"
    command = [
        sys.executable,
        str(crawler),
        "--site-url", str(config["site_url"]),
        "--rules", str(config["rules"]),
        "--out-dir", str(config["output_dir"]),
        "--delay", str(config["delay"]),
    ]
    for category in config.get("categories", []):
        command.extend(["--category", str(category)])
    if config.get("exact_categories"):
        command.append("--exact-category")
    if config.get("max_products") is not None:
        command.extend(["--max-products", str(config["max_products"])])
    return command


def run_once(config: dict[str, object]) -> None:
    output_dir = Path(str(config["output_dir"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    command = _crawl_command(config)
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise RuntimeError(f"crawl failed with exit code {completed.returncode}")


def _configure(args: argparse.Namespace) -> int:
    _parse_time(args.time)
    _timezone(args.timezone)
    if args.delay < 0:
        raise ValueError("delay must not be negative")
    if args.max_products is not None and args.max_products < 1:
        raise ValueError("max-products must be at least 1")
    output_dir = Path(args.output_dir).expanduser().resolve()
    config = {
        "day": args.day.casefold(),
        "time": args.time,
        "timezone": args.timezone,
        "site_url": args.site_url,
        "rules": str(Path(args.rules).expanduser().resolve()),
        "output_dir": str(output_dir),
        "delay": args.delay,
        "categories": args.category,
        "max_products": args.max_products,
    }
    next_run(datetime.now().astimezone(), **{
        "day": config["day"],
        "run_time": config["time"],
        "timezone": config["timezone"],
    })
    schedule_file = Path(args.schedule_file).expanduser()
    _write_config(schedule_file, config)
    print(f"Saved weekly schedule to {schedule_file}")
    print(f"Runs every {config['day']} at {config['time']} ({config['timezone']})")
    print(f"Output directory: {output_dir}")
    return 0


def _run(args: argparse.Namespace) -> int:
    config = _read_config(Path(args.schedule_file).expanduser())
    if args.once:
        run_once(config)
        return 0

    while True:
        target = next_run(
            datetime.now().astimezone(),
            day=str(config["day"]),
            run_time=str(config["time"]),
            timezone=str(config["timezone"]),
        )
        seconds = max(0, (target - datetime.now(target.tzinfo)).total_seconds())
        print(f"Next crawl: {target.isoformat()}", flush=True)
        time.sleep(seconds)
        try:
            run_once(config)
        except Exception as error:
            print(f"Crawl failed: {error}", file=sys.stderr, flush=True)
        else:
            print("Crawl completed; waiting for the next scheduled run.", flush=True)


def _show(args: argparse.Namespace) -> int:
    config = _read_config(Path(args.schedule_file).expanduser())
    print(json.dumps(config, indent=2))
    print("Next run:", next_run(
        datetime.now().astimezone(),
        day=str(config["day"]),
        run_time=str(config["time"]),
        timezone=str(config["timezone"]),
    ).isoformat())
    return 0


def _serve(args: argparse.Namespace) -> int:
    from deploy.dashboard import serve

    config = _read_config(Path(args.schedule_file).expanduser())
    scheduler = threading.Thread(target=_run, args=(argparse.Namespace(
        schedule_file=args.schedule_file, once=False,
    ),), daemon=True)
    scheduler.start()
    serve(config, args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="persist the weekly crawl settings")
    configure.add_argument("--schedule-file", default=str(DEFAULT_SCHEDULE_FILE))
    configure.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    configure.add_argument("--day", default="sunday", choices=DAY_NAMES)
    configure.add_argument("--time", default="12:00", help="24-hour local time, HH:MM")
    configure.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    configure.add_argument("--site-url", default="https://panteltactical.com")
    configure.add_argument("--rules", default="/app/washington-rules.csv")
    configure.add_argument("--delay", type=float, default=0.5)
    configure.add_argument("--category", action="append", default=[])
    configure.add_argument("--max-products", type=int)
    configure.set_defaults(handler=_configure)

    for name, handler in (("run", _run), ("show", _show)):
        command = subparsers.add_parser(name, help=f"{name} the persisted schedule")
        command.add_argument("--schedule-file", default=str(DEFAULT_SCHEDULE_FILE))
        if name == "run":
            command.add_argument("--once", action="store_true", help="run immediately and exit")
        command.set_defaults(handler=handler)
    server = subparsers.add_parser("serve", help="run the dashboard and weekly scheduler")
    server.add_argument("--schedule-file", default=str(DEFAULT_SCHEDULE_FILE))
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=8080)
    server.set_defaults(handler=_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
