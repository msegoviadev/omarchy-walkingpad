#!/usr/bin/env python3
"""Per-day rollup of WalkingPad history.

The widget only ever displays day granularity, so records older than today
are compacted into rollup.json (rebuilt by the collector at startup and at
day rollover) and then dropped from history.jsonl, which therefore only ever
holds today's uncompacted records. stats.py reads the small rollup plus
today's raw tail instead of re-parsing a growing history on every poll.

Rollup layout:
  {"version": 1,
   "through": "YYYY-MM-DD",        # last fully compacted day (<= yesterday)
   "last_live_ts": 1787...,        # newest live record ts in the compacted
                                   # range; seeds the tail's final-dedup
   "carry": {"session": [steps, dist_m, time_s]},  # last counter values of
                                   # sessions near the boundary, so a session
                                   # spanning midnight is not double counted
                                   # when the tail re-reads its counters
   "days": {"YYYY-MM-DD": {
       "steps", "dist_m", "time_s", "sessions",
       "avg_speed",                # dist/time in km/h, time-weighted
       "median_speed", "max_speed",  # over live samples with speed > 0
       "active_s",                 # belt time with step progress
       "longest_session_s"}}}
  Days recovered only from pad "final" summaries have null speed fields.
"""

import json
import os
import statistics
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "walkingpad"
HISTORY_FILE = DATA_DIR / "history.jsonl"
ROLLUP_FILE = DATA_DIR / "rollup.json"

FINAL_DUPLICATE_WINDOW = 6 * 3600  # a "final" record near live records is one
# we recorded ourselves; skip it to avoid double counting


def local_date(ts: float) -> date:
    return datetime.fromtimestamp(ts).date()


def load_records(since: date | None = None) -> list:
    """Parse history records. With `since`, lines older than that day are
    skipped via their timestamp prefix without JSON parsing."""
    cutoff = None
    if since is not None:
        cutoff = datetime.combine(since, datetime.min.time()).timestamp()
    records = []
    try:
        with open(HISTORY_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if cutoff is not None:
                    # Every record is written with "ts" as the first key.
                    try:
                        ts = float(line[6 : line.index(",", 6)])
                    except (ValueError, IndexError):
                        continue
                    if ts < cutoff:
                        continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return records


def _new_day():
    return {
        "steps": 0,
        "dist_m": 0,
        "time_s": 0,
        "sessions": set(),
        "speeds": [],
        "active_s": 0,
        "session_time": defaultdict(int),
    }


def aggregate_days(
    records: list,
    seed_live_ts: list | None = None,
    seed_prev: dict | None = None,
) -> dict:
    """Per-day stats from raw records: {date: {...metrics...}}.

    Steps/distance/time are summed from within-session counter deltas, so
    sessions spanning midnight attribute correctly and pad counter resets
    never subtract. `seed_live_ts` carries live timestamps from an already
    compacted range so a "final" record in the tail is still recognised as
    a duplicate of a compacted run; `seed_prev` carries the last counter
    values per session so a session spanning the rollup boundary keeps its
    delta chain instead of re-counting its full counter.
    """
    records = sorted(records, key=lambda r: float(r.get("ts", 0)))
    live_ts = sorted(seed_live_ts or [])
    days = defaultdict(_new_day)
    prev_by_session = {s: tuple(vals) for s, vals in (seed_prev or {}).items()}

    for rec in records:
        ts = float(rec.get("ts", 0))
        day = local_date(ts)

        if rec.get("type") == "final":
            # Pad-stored last-run summary. Only count it when no live record
            # exists in the hours before it, meaning the daemon missed that
            # run entirely (service was off, KS Fit had the link, ...).
            idx = bisect_left(live_ts, ts) - 1  # newest live ts strictly < ts
            duplicate = idx >= 0 and ts - live_ts[idx] < FINAL_DUPLICATE_WINDOW
            if not duplicate:
                stats = days[day]
                stats["steps"] += int(rec.get("steps", 0))
                stats["dist_m"] += int(rec.get("dist_m", 0))
                stats["time_s"] += int(rec.get("time_s", 0))
                stats["sessions"].add(f"final-{ts}")
                stats["session_time"][f"final-{ts}"] += int(rec.get("time_s", 0))
            continue

        session = rec.get("session")
        if not session:
            continue
        live_ts.append(ts)  # records are ts-sorted, appends stay ordered
        cur = (
            int(rec.get("steps", 0)),
            int(rec.get("dist_m", 0)),
            int(rec.get("time_s", 0)),
        )
        # Pad counters start at zero each run, so the first record we see is
        # itself a delta; it also covers steps walked just before we connected.
        prev = prev_by_session.get(session)
        delta = cur if prev is None else tuple(max(0, c - p) for c, p in zip(cur, prev))
        prev_by_session[session] = cur

        stats = days[day]
        stats["sessions"].add(session)
        stats["steps"] += delta[0]
        stats["dist_m"] += delta[1]
        stats["time_s"] += delta[2]
        if delta[0] > 0:
            stats["active_s"] += max(1, delta[2])
        stats["session_time"][session] += delta[2]

        speed = float(rec.get("speed") or 0)
        if rec.get("state") == 1 and speed > 0:
            stats["speeds"].append(speed)

    result = {}
    for day, stats in days.items():
        dist, duration, speeds = stats["dist_m"], stats["time_s"], stats["speeds"]
        result[day] = {
            "steps": stats["steps"],
            "dist_m": dist,
            "time_s": duration,
            "sessions": len(stats["sessions"]),
            # m/s -> km/h; the time-weighted "how fast did I actually walk".
            "avg_speed": round(dist / duration * 3.6, 1) if duration > 0 else None,
            "median_speed": round(statistics.median(speeds), 1) if speeds else None,
            "max_speed": round(max(speeds), 1) if speeds else None,
            "active_s": stats["active_s"],
            "longest_session_s": max(stats["session_time"].values(), default=0),
        }
    return result


def load_rollup() -> dict | None:
    try:
        with open(ROLLUP_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def rebuild_needed(today: date | None = None) -> bool:
    rollup = load_rollup()
    if not rollup:
        return True
    try:
        through = date.fromisoformat(str(rollup.get("through", "")))
    except ValueError:
        return True
    return through < (today or date.today()) - timedelta(days=1)


def truncate_history(today: date) -> int:
    """Drop records older than `today` from history.jsonl. rollup.json must
    already cover them (rebuild_rollup writes it first). Atomic via
    tmp+rename; returns the number of lines dropped. Unparseable lines are
    kept rather than silently lost."""
    cutoff = datetime.combine(today, datetime.min.time()).timestamp()
    kept, dropped = [], 0
    try:
        with open(HISTORY_FILE) as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                # Every record is written with "ts" as the first key.
                try:
                    ts = float(stripped[6 : stripped.index(",", 6)])
                except (ValueError, IndexError):
                    kept.append(line)
                    continue
                if ts >= cutoff:
                    kept.append(line)
                else:
                    dropped += 1
    except OSError:
        return 0
    tmp = HISTORY_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        fh.writelines(kept)
    os.replace(tmp, HISTORY_FILE)
    return dropped


def rebuild_rollup(today: date | None = None) -> tuple[dict, int]:
    """Extend rollup.json with all history up to yesterday, then truncate
    those records from history.jsonl. Returns (rollup, lines dropped).

    Incremental: history only holds uncompacted records, so each build
    merges newly compacted days into the existing rollup. The previous
    rollup's carry seeds the delta chains of sessions spanning the old
    boundary, and its last_live_ts seeds final-record dedup.

    Crash safety: the rollup lands on disk before truncation, and both
    writes are atomic renames, so a crash at any point leaves rollup and
    history consistent (worst case the same records live in both, which the
    next rebuild just compacts again).
    """
    today = today or date.today()
    through = today - timedelta(days=1)

    existing = load_rollup() or {}
    try:
        existing_through = date.fromisoformat(str(existing.get("through", "")))
    except ValueError:
        existing_through = None
    if existing_through is not None and existing_through >= through:
        return existing, 0  # already current

    records = load_records()
    if existing_through is not None:
        # Only records newer than the existing rollup are uncompacted.
        new_records = [r for r in records if local_date(float(r.get("ts", 0))) > existing_through]
        seed_live = [float(existing.get("last_live_ts", 0))]
        seed_prev = existing.get("carry", {})
    else:
        new_records = records
        seed_live, seed_prev = [], {}

    compacted = [r for r in new_records if local_date(float(r.get("ts", 0))) <= through]
    new_days = aggregate_days(compacted, seed_live_ts=seed_live, seed_prev=seed_prev)

    days = dict(existing.get("days", {}))
    for day, stats in sorted(new_days.items()):
        days[day.isoformat()] = stats

    live_times = [
        float(r.get("ts", 0)) for r in compacted if r.get("type") != "final" and r.get("session")
    ]
    last_live_ts = max([float(existing.get("last_live_ts", 0)), *live_times])

    # Counter state per session near the new boundary, so the next tail can
    # continue delta chains of sessions spanning midnight. Sessions idle for
    # over 6h are closed (a session never survives a reconnect) and pruned.
    carry = {}
    for rec in compacted:
        if rec.get("type") == "final" or not rec.get("session"):
            continue
        if last_live_ts - float(rec.get("ts", 0)) > FINAL_DUPLICATE_WINDOW:
            continue
        carry[rec["session"]] = [
            int(rec.get("steps", 0)),
            int(rec.get("dist_m", 0)),
            int(rec.get("time_s", 0)),
        ]

    rollup = {
        "version": 1,
        "through": through.isoformat(),
        "last_live_ts": last_live_ts,
        "carry": carry,
        "days": days,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ROLLUP_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(rollup, fh, separators=(",", ":"))
    os.replace(tmp, ROLLUP_FILE)
    dropped = truncate_history(today)
    return rollup, dropped


if __name__ == "__main__":
    built, dropped = rebuild_rollup()
    print(json.dumps(built, indent=2))
    print(f"compacted {dropped} history lines", file=sys.stderr)
