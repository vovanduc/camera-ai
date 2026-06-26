from datetime import datetime, date

from counting import bucket_hourly, bucket_daily, summarize


def _c(iso: str, direction: str) -> dict:
    return {"ts": datetime.fromisoformat(iso), "direction": direction}


def test_hourly_converts_utc_to_vn_hour():
    # 01:30 UTC == 08:30 VN -> hour 8
    rows = bucket_hourly([_c("2026-06-23T01:30:00+00:00", "in")], date(2026, 6, 23))
    assert rows[8]["in"] == 1
    assert sum(r["in"] for r in rows) == 1
    assert len(rows) == 24


def test_hourly_separates_in_out():
    rows = bucket_hourly(
        [
            _c("2026-06-23T02:00:00+00:00", "in"),
            _c("2026-06-23T02:10:00+00:00", "in"),
            _c("2026-06-23T02:20:00+00:00", "out"),
        ],
        date(2026, 6, 23),
    )
    assert rows[9]["in"] == 2   # 02 UTC == 09 VN
    assert rows[9]["out"] == 1


def test_hourly_vn_midnight_boundary():
    # 2026-06-22T17:30Z == 2026-06-23T00:30 VN -> belongs to the 23rd, hour 0
    c = [_c("2026-06-22T17:30:00+00:00", "in")]
    assert bucket_hourly(c, date(2026, 6, 23))[0]["in"] == 1
    assert sum(r["in"] for r in bucket_hourly(c, date(2026, 6, 22))) == 0


def test_daily_fills_range():
    rows = bucket_daily(
        [_c("2026-06-23T02:00:00+00:00", "in")], date(2026, 6, 21), date(2026, 6, 23)
    )
    assert len(rows) == 3
    assert rows[0]["date"] == date(2026, 6, 21) and rows[0]["in"] == 0
    assert rows[2]["date"] == date(2026, 6, 23) and rows[2]["in"] == 1


def test_summarize_occupancy():
    s = summarize(
        [
            _c("2026-06-23T02:00:00+00:00", "in"),
            _c("2026-06-23T02:00:00+00:00", "in"),
            _c("2026-06-23T03:00:00+00:00", "out"),
        ]
    )
    assert s == {"in": 2, "out": 1, "occupancy": 1}
