#!/usr/bin/env python3
"""WalkingPad stats helper for the Omarchy bar widget.

Reads the per-day rollup plus today's raw history tail (see rollup.py) and
the collector's live status, and prints one JSON blob:
  {
    "enabled":   bool,                       # systemd service state
    "connected": bool, "walking": bool,      # live pad state (fresh only)
    "live":      {"steps", "dist_m", "time_s", "speed", "session_start"},
    "today":     {"steps", "dist_m", "time_s", "sessions", ...},
    "start":     "YYYY-MM-DD",               # Monday of the first grid week
    "days":      {"YYYY-MM-DD": {"steps", "dist_m", "time_s", "sessions",
                   "avg_speed", "median_speed", "max_speed",
                   "active_s", "longest_session_s"}, ...},
    "streak":    int,                        # consecutive goal days (or active
                                             # days when no goal is set)
    "totals":    {"steps", "dist_m", "time_s", "sessions", "avg_speed",
                  "longest_session_s"},
    "devices":   [{"name", "address", "rssi", "protocol", "last_seen"}...],
    "selected_address": "AA:BB:.." | ""      # "" = auto (strongest signal)
  }

Speed fields are null for days recovered only from pad "final" summaries
(no per-second samples exist for those).
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import safeio
from rollup import aggregate_days, load_records, load_rollup

DATA_DIR = Path.home() / ".local" / "share" / "walkingpad"
STATUS_FILE = DATA_DIR / "status.json"
DEVICES_FILE = DATA_DIR / "devices.json"
CONFIG_FILE = Path.home() / ".config" / "walkingpad" / "config.json"

GRID_WEEKS = 15
LIVE_MAX_AGE = 10.0  # seconds before status.json counts as stale
MAX_STATE_BYTES = 64 * 1024  # status, devices, config are all tiny by design
MAX_OUTPUT_BYTES = 256 * 1024  # the shell buffers our whole stdout; backstop

EMPTY_DAY = {
    "steps": 0,
    "dist_m": 0,
    "time_s": 0,
    "sessions": 0,
    "avg_speed": None,
    "median_speed": None,
    "max_speed": None,
    "active_s": 0,
    "longest_session_s": 0,
}


def all_days() -> dict:
    """{date: day stats}: rollup days plus a fresh aggregation of the raw
    records newer than the rollup (today's tail). Falls back to a full scan
    when no usable rollup exists yet."""
    today = date.today()
    rollup = load_rollup()
    through = None
    if rollup:
        try:
            through = date.fromisoformat(str(rollup.get("through", "")))
        except ValueError:
            through = None

    if through is None or through >= today:
        return aggregate_days(load_records())

    days = {date.fromisoformat(iso): stats for iso, stats in rollup.get("days", {}).items()}
    tail = load_records(since=through + timedelta(days=1))
    # Rollup and tail days are disjoint by construction (through < today),
    # so a plain update is a merge, not an overwrite.
    days.update(
        aggregate_days(
            tail,
            seed_live_ts=[float(rollup.get("last_live_ts", 0))],
            seed_prev=rollup.get("carry", {}),
        )
    )
    return days


def compute_streak(days, goal: int) -> int:
    def hit(day: date) -> bool:
        steps = days.get(day, {}).get("steps", 0)
        return steps >= goal if goal > 0 else steps > 0

    cursor = date.today()
    if not hit(cursor):
        cursor -= timedelta(days=1)  # today still in progress; anchor yesterday
    streak = 0
    while hit(cursor):
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def service_enabled() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", "walkingpad.service"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def live_status():
    status = safeio.read_json(STATUS_FILE, MAX_STATE_BYTES)
    if not isinstance(status, dict):
        return None
    if time.time() - float(status.get("ts", 0)) > LIVE_MAX_AGE:
        return None
    if not status.get("connected"):
        return None
    return status


def known_devices():
    """Pads seen by the collector, strongest signal first."""
    devices = safeio.read_json(DEVICES_FILE, MAX_STATE_BYTES)
    if not isinstance(devices, dict):
        return []
    return sorted(devices.values(), key=lambda d: d.get("rssi") or -999, reverse=True)


def selected_address() -> str:
    config = safeio.read_json(CONFIG_FILE, MAX_STATE_BYTES)
    if not isinstance(config, dict):
        return ""
    return str(config.get("device_address", "")).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", type=int, default=0, help="daily step goal")
    args = parser.parse_args()

    days = all_days()

    today = date.today()
    grid_start = today - timedelta(weeks=GRID_WEEKS - 1)
    grid_start -= timedelta(days=grid_start.weekday())  # back to Monday

    live = live_status()
    today_stats = dict(days.get(today, EMPTY_DAY))

    totals = dict(EMPTY_DAY)
    for day_stats in days.values():
        totals["steps"] += day_stats["steps"]
        totals["dist_m"] += day_stats["dist_m"]
        totals["time_s"] += day_stats["time_s"]
        # Sessions spanning midnight count once per day they touched.
        totals["sessions"] += day_stats["sessions"]
        totals["active_s"] += day_stats["active_s"]
        totals["longest_session_s"] = max(
            totals["longest_session_s"], day_stats["longest_session_s"]
        )
    if totals["time_s"] > 0:
        totals["avg_speed"] = round(totals["dist_m"] / totals["time_s"] * 3.6, 1)

    out = json.dumps(
        {
            "enabled": service_enabled(),
            "connected": bool(live),
            "walking": bool(live and live.get("walking")),
            "live": {
                "steps": live.get("steps", 0),
                "dist_m": live.get("dist_m", 0),
                "time_s": live.get("time_s", 0),
                "speed": live.get("speed", 0.0),
                "session_start": live.get("session_start"),
                "device_name": live.get("device_name", ""),
                "address": live.get("address", ""),
            }
            if live
            else None,
            "today": today_stats,
            "start": grid_start.isoformat(),
            "days": {
                day.isoformat(): stats
                for day, stats in days.items()
                if day >= grid_start and stats["steps"] > 0
            },
            "streak": compute_streak(days, args.goal),
            "totals": totals,
            "devices": known_devices(),
            "selected_address": selected_address(),
        }
    )
    # The shell buffers our complete stdout; never emit an oversized blob.
    if len(out) > MAX_OUTPUT_BYTES:
        sys.exit("stats output exceeds size cap")
    print(out)


if __name__ == "__main__":
    sys.exit(main())
