"""Pure crossing-event bucketing. Stdlib only — unit-testable, no DB.

A crossing = {"ts": aware datetime (UTC from DB), "direction": "in"|"out"}.
Counting = COUNT of crossings, bucketed in Asia/Ho_Chi_Minh (UTC+7).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

VN = ZoneInfo("Asia/Ho_Chi_Minh")


def _vn_dt(ts: datetime) -> datetime:
    return ts.astimezone(VN)


def bucket_hourly(crossings: list[dict], day: date) -> list[dict]:
    """24 rows for the given VN day."""
    buckets = [{"hour": h, "in": 0, "out": 0} for h in range(24)]
    for c in crossings:
        t = _vn_dt(c["ts"])
        if t.date() != day:
            continue
        if c["direction"] in ("in", "out"):
            buckets[t.hour][c["direction"]] += 1
    return buckets


def bucket_daily(crossings: list[dict], since: date, until: date) -> list[dict]:
    """One row per VN date in [since, until], zero-filled."""
    days: dict[date, dict] = {}
    cur = since
    while cur <= until:
        days[cur] = {"date": cur, "in": 0, "out": 0}
        cur += timedelta(days=1)
    for c in crossings:
        d = _vn_dt(c["ts"]).date()
        if d in days and c["direction"] in ("in", "out"):
            days[d][c["direction"]] += 1
    return [days[k] for k in sorted(days)]


def summarize(crossings: list[dict]) -> dict:
    ins = sum(1 for c in crossings if c["direction"] == "in")
    outs = sum(1 for c in crossings if c["direction"] == "out")
    return {"in": ins, "out": outs, "occupancy": ins - outs}
