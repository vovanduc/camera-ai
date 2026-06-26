# Phase 1: Module ĐẾM người ra/vào — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Đưa logic ĐẾM người ra/vào (đã chạy prod ở repo `dcnet-cloud/camera`) vào camera-ai: cam Axis phát crossline IN/OUT qua MQTT → service `event_collector` ghi bảng `events` vào Postgres chung → app FDW hiển thị occupancy + IN/OUT hôm nay + log + chart theo giờ.

**Architecture:** `event_collector` = service async riêng (aiomqtt + asyncpg), store-only, idempotent INSERT vào `events`. FDW app (FastAPI + psycopg sync) chỉ ĐỌC `events`/`cameras` để render. 2 style DB chung 1 schema Postgres (đã chấp nhận ở migration spec). Counting = COUNT rows tại query-time, bucket VN+7 (pure stdlib `counting.py`).

**Tech Stack:** Python 3.12; collector: aiomqtt 2.3.0 + asyncpg 0.30.0 + structlog 24.4.0; FDW: FastAPI + psycopg 3 (sync, ConnectionPool, dict_row) + Jinja2 templates; PostgreSQL 16 (pgvector/pg16); Docker Compose.

## Global Constraints

- **Schema thêm vào `init_db()`** trong `fall_detection_web/db.py` bằng `CREATE TABLE IF NOT EXISTS` (camera-ai KHÔNG có file .sql, KHÔNG Alembic — Phase 0 pattern).
- **Tên bảng `events`** giờ TRỐNG (Phase 0 đã đổi FDW `events`→`incidents`) → coexist sạch với `incidents`.
- **`MQTT_CLIENT_ID` mặc định = `event_collector_cameraai`** — DUY NHẤT, không kick collector DCNET prod đang đọc cùng broker.
- **Broker = cloud đọc ké:** `camera-test.dcnet.vn:8883`, TLS on (`MQTT_TLS=true`), system CA.
- **Counting query-time, VN+7:** đếm số event ROW, KHÔNG dùng `totalHuman`. Occupancy = COUNT(IN today) − COUNT(OUT today), **clamp ≥ 0** (quyết định review §9).
- **Seed cam Axis idempotent** bằng SQL `ON CONFLICT DO NOTHING` trong `init_db()` (quyết định review §9).
- **Idempotent INSERT:** `ON CONFLICT (cam_id, axis_object_id, ts, direction) DO NOTHING` → reboot/duplicate không tạo row giả.
- **Route `/counting` PHẢI khai báo TRƯỚC catch-all `@app.get("/{page_name}")`** (app.py:152) — FastAPI match theo thứ tự khai báo.
- **psycopg query pattern:** `with get_conn() as conn: conn.execute(sql, params).fetchall()`, `row_factory=dict_row` (pool đã set). Dùng `%s` placeholder.
- **KHÔNG đổi hàm db.py cũ.** Chỉ thêm.

---

## File Structure

**Create (collector service):**
- `services/event_collector/src/event_collector/__init__.py` — empty package marker
- `services/event_collector/src/event_collector/parser.py` — Axis MQTT topic → normalized event dict
- `services/event_collector/src/event_collector/repo.py` — asyncpg INSERT-only (`cam_id_for`, `ensure_cam`, `insert_counter`)
- `services/event_collector/src/event_collector/main.py` — MQTT consume loop + dispatcher (store-only)
- `services/event_collector/tests/test_parser.py` — pure unit test topic→direction
- `services/event_collector/Dockerfile`
- `services/event_collector/requirements.txt`

**Create (counting logic + UI):**
- `fall_detection_web/counting.py` — pure stdlib bucketing (`bucket_hourly`, `bucket_daily`, `summarize`)
- `fall_detection_web/tests/test_counting.py` — pure unit test
- `fall_detection_web/templates/counting.html` — trang "Đếm ra/vào"

**Modify:**
- `fall_detection_web/db.py` — `init_db()` thêm `cameras`+`events`+seed; thêm hàm `list_cameras`, `cam_id_for`, `counting_occupancy_today`, `counting_crossings`
- `fall_detection_web/app.py` — route `GET /counting` + `GET /api/counting` (trước catch-all)
- `fall_detection_web/templates/index.html` — nav link "Đếm ra/vào"
- `docker-compose.yml` — service `event_collector`

---

## Task 1: Schema counting (cameras + events) + seed cam Axis

Thêm 2 bảng vào `init_db()` + seed cam Axis idempotent + 4 hàm query đọc. Không có UI, không có collector — task này verify bằng smoke test `init_db()` chạy được + bảng tồn tại + query trả 0/empty khi rỗng.

**Files:**
- Modify: `fall_detection_web/db.py` (thêm vào cuối `init_db()` ~line 119; thêm hàm mới ở cuối file)

**Interfaces:**
- Consumes: `get_conn()` (db.py:71), `dict_row` pool, `LOCAL_TZ` (db.py — UTC+7)
- Produces:
  - `list_cameras() -> list[dict]` — mỗi dict có keys `id, cam_uid, name, model, location, enabled`
  - `cam_id_for(cam_uid: str) -> int | None`
  - `counting_occupancy_today(cam_id: int | None = None) -> dict` — keys `in: int, out: int, occupancy: int` (occupancy clamp ≥0)
  - `counting_crossings(day: date, cam_id: int | None = None) -> list[dict]` — mỗi dict có keys `ts: datetime (aware UTC), direction: str`

- [ ] **Step 1: Thêm schema + seed vào `init_db()`**

Thêm vào CUỐI thân `with get_conn() as conn:` trong `init_db()` (sau khối `settings`, db.py:119):

```python
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cameras (
                id          SERIAL PRIMARY KEY,
                cam_uid     TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                rtsp_url    TEXT NOT NULL,
                mjpeg_url   TEXT,
                vendor      TEXT DEFAULT 'axis',
                model       TEXT,
                location    TEXT,
                enabled     BOOLEAN DEFAULT true,
                created_at  TIMESTAMPTZ DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id              BIGSERIAL PRIMARY KEY,
                cam_id          INT REFERENCES cameras(id),
                ts              TIMESTAMPTZ NOT NULL,
                type            TEXT NOT NULL,
                direction       TEXT,
                axis_object_id  TEXT,
                payload         JSONB NOT NULL,
                snapshot_path   TEXT,
                face_path       TEXT,
                face_score      REAL,
                created_at      TIMESTAMPTZ DEFAULT now(),
                UNIQUE (cam_id, axis_object_id, ts, direction)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS events_cam_ts ON events (cam_id, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS events_type_ts ON events (type, ts DESC)")
        conn.execute("""
            INSERT INTO cameras (cam_uid, name, rtsp_url, model, location)
            VALUES ('B8A44F4627CE', 'Cửa cty HCM',
                    'rtsp://192.168.100.47/axis-media/media.amp', 'M3216-LVE', 'HCM')
            ON CONFLICT (cam_uid) DO NOTHING
        """)
```

- [ ] **Step 2: Thêm 4 hàm query ở cuối db.py**

Thêm vào cuối `fall_detection_web/db.py` (import `date` nếu chưa — db.py đã import `datetime, timedelta, timezone` từ `datetime`; thêm `date` vào dòng import đó):

```python
# ── Counting (Phase 1) ──

def list_cameras() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, cam_uid, name, model, location, enabled "
            "FROM cameras ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def cam_id_for(cam_uid: str) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM cameras WHERE cam_uid = %s", (cam_uid,)
        ).fetchone()
    return int(row["id"]) if row else None


def counting_occupancy_today(cam_id: int | None = None) -> dict[str, int]:
    """IN/OUT/occupancy của NGÀY VN hiện tại. occupancy clamp >= 0."""
    where = "type = 'counter' AND (ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date " \
            "= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date"
    params: tuple[Any, ...] = ()
    if cam_id is not None:
        where += " AND cam_id = %s"
        params = (cam_id,)
    sql = (
        "SELECT "
        "COUNT(*) FILTER (WHERE direction = 'in')  AS ins, "
        "COUNT(*) FILTER (WHERE direction = 'out') AS outs "
        f"FROM events WHERE {where}"
    )
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    ins = int(row["ins"] or 0)
    outs = int(row["outs"] or 0)
    return {"in": ins, "out": outs, "occupancy": max(0, ins - outs)}


def counting_crossings(day: date, cam_id: int | None = None) -> list[dict[str, Any]]:
    """Crossing rows của 1 ngày VN — cho bucket_hourly + log. ts trả về aware UTC."""
    where = "type = 'counter' AND (ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date = %s"
    params: list[Any] = [day]
    if cam_id is not None:
        where += " AND cam_id = %s"
        params.append(cam_id)
    sql = f"SELECT ts, direction FROM events WHERE {where} ORDER BY ts DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [{"ts": r["ts"], "direction": r["direction"]} for r in rows]
```

- [ ] **Step 3: Smoke test — init_db tạo bảng + query rỗng trả 0**

Chạy (compose postgres phải up — `docker compose up -d postgres`):

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai/fall_detection_web
DATABASE_URL=postgresql://dcnet:dcnet_dev@localhost:5432/dcnet python3 -c "
import db
from datetime import date, timezone, timedelta, datetime
db.init_db()
print('cameras:', db.list_cameras())
print('cam_id B8A44F4627CE:', db.cam_id_for('B8A44F4627CE'))
print('occ:', db.counting_occupancy_today())
vn_today = datetime.now(timezone(timedelta(hours=7))).date()
print('crossings today:', db.counting_crossings(vn_today))
"
```

Expected: `cameras:` có 1 dict cam_uid `B8A44F4627CE`; `cam_id` = int; `occ:` `{'in': 0, 'out': 0, 'occupancy': 0}`; `crossings today:` `[]`. Không exception.

- [ ] **Step 4: Verify seed idempotent (chạy init_db 2 lần không nhân đôi)**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai/fall_detection_web
DATABASE_URL=postgresql://dcnet:dcnet_dev@localhost:5432/dcnet python3 -c "
import db
db.init_db(); db.init_db()
print('cam count:', len(db.list_cameras()))
"
```

Expected: `cam count: 1`.

- [ ] **Step 5: Commit**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
git add fall_detection_web/db.py
git commit -m "feat(phase1): schema cameras+events + seed Axis + counting queries"
```

---

## Task 2: `counting.py` pure bucketing + unit tests

Port nguyên `counting.py` từ DCNET (pure stdlib, VN+7) + port test. TDD: viết test trước (đã có sẵn từ DCNET), chạy fail, port code, chạy pass.

**Files:**
- Create: `fall_detection_web/counting.py`
- Create: `fall_detection_web/tests/test_counting.py`

**Interfaces:**
- Produces:
  - `bucket_hourly(crossings: list[dict], day: date) -> list[dict]` — 24 rows, mỗi row `{hour, in, out}`
  - `bucket_daily(crossings: list[dict], since: date, until: date) -> list[dict]` — 1 row/ngày `{date, in, out}`
  - `summarize(crossings: list[dict]) -> dict` — `{in, out, occupancy}`
  - crossing = `{"ts": aware datetime, "direction": "in"|"out"}`

- [ ] **Step 1: Viết test (port từ DCNET)**

Create `fall_detection_web/tests/test_counting.py`:

```python
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
```

- [ ] **Step 2: Chạy test — verify FAIL**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai/fall_detection_web && python3 -m pytest tests/test_counting.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'counting'` (chưa có file). Nếu thiếu pytest: `pip install pytest`.

- [ ] **Step 3: Port `counting.py`**

Create `fall_detection_web/counting.py`:

```python
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
```

- [ ] **Step 4: Chạy test — verify PASS**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai/fall_detection_web && python3 -m pytest tests/test_counting.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
git add fall_detection_web/counting.py fall_detection_web/tests/test_counting.py
git commit -m "feat(phase1): port counting.py pure bucketing + unit tests"
```

---

## Task 3: `event_collector` service (parser + repo + main + Docker + compose) + parser test

Port 3 file collector từ DCNET với 2 adaptation: (a) `repo.ensure_cam` BỎ insert vào `occupancy` (bảng đó KHÔNG có ở Phase 1 — occupancy là derived); (b) `main._dsn()` ưu tiên `DATABASE_URL` (compose camera-ai dùng env này). TDD cho parser (pure).

**Files:**
- Create: `services/event_collector/src/event_collector/__init__.py`
- Create: `services/event_collector/src/event_collector/parser.py`
- Create: `services/event_collector/src/event_collector/repo.py`
- Create: `services/event_collector/src/event_collector/main.py`
- Create: `services/event_collector/tests/test_parser.py`
- Create: `services/event_collector/Dockerfile`
- Create: `services/event_collector/requirements.txt`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: bảng `cameras`/`events` (Task 1); cloud broker MQTT.
- Produces:
  - `parse_event(topic: str, payload: dict) -> dict | None` — keys `type, cam_uid, ts, direction, scenario, data, raw`
  - `Repo.cam_id_for(cam_uid) -> int | None`, `Repo.ensure_cam(cam_uid, name, rtsp_url) -> int`, `Repo.insert_counter(...) -> int | None`

- [ ] **Step 1: Viết parser test**

Create `services/event_collector/tests/test_parser.py`:

```python
from datetime import timezone

from event_collector.parser import parse_event


def _payload(ts_ms: int = 1_700_000_000_000) -> dict:
    return {"timestamp": ts_ms, "serial": "B8A44F4627CE",
            "message": {"source": {}, "key": {}, "data": {"totalHuman": 3}}}


def test_scenario1_is_in():
    topic = "axis/B8A44F4627CE/event/.../ObjectAnalytics/Device1Scenario1"
    ev = parse_event(topic, _payload())
    assert ev["type"] == "counter"
    assert ev["direction"] == "in"
    assert ev["cam_uid"] == "B8A44F4627CE"
    assert ev["ts"].tzinfo == timezone.utc


def test_scenario2_is_out():
    topic = "axis/B8A44F4627CE/event/.../ObjectAnalytics/Device1Scenario2"
    ev = parse_event(topic, _payload())
    assert ev["direction"] == "out"


def test_interval_topic_ignored_as_counter():
    topic = "axis/B8A44F4627CE/.../ObjectAnalytics/Device1Scenario1Interval"
    ev = parse_event(topic, _payload())
    # 'Interval' loại khỏi counter; topic không match motion/health -> None
    assert ev is None


def test_bad_timestamp_returns_none():
    assert parse_event("axis/x/.../Device1Scenario1", {"no": "timestamp"}) is None


def test_unknown_topic_returns_none():
    assert parse_event("axis/x/event/Something/Else", _payload()) is None
```

- [ ] **Step 2: Tạo package + chạy test — verify FAIL**

```bash
mkdir -p /Users/vovanduc/Code/dcnet/camera-ai/services/event_collector/src/event_collector
mkdir -p /Users/vovanduc/Code/dcnet/camera-ai/services/event_collector/tests
touch /Users/vovanduc/Code/dcnet/camera-ai/services/event_collector/src/event_collector/__init__.py
cd /Users/vovanduc/Code/dcnet/camera-ai/services/event_collector && PYTHONPATH=src python3 -m pytest tests/test_parser.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'event_collector.parser'`.

- [ ] **Step 3: Port `parser.py` (verbatim)**

Create `services/event_collector/src/event_collector/parser.py`:

```python
"""Parse Axis MQTT events into typed dicts the repo can persist.

Topic structure on broker:

    axis/<SERIAL>/event/<NAMESPACE_PREFIX>/<EVENT_PATH>
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _cam_uid_from_topic(topic: str) -> str:
    parts = topic.split("/")
    return parts[1] if len(parts) >= 2 else "unknown"


def _ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)


def parse_event(topic: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a normalized event dict, or None for unsupported topics."""
    cam_uid = _cam_uid_from_topic(topic)
    try:
        ts = _ms_to_dt(payload["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    data = (payload.get("message") or {}).get("data") or {}

    # Counter — Object Analytics line crossing.
    if "ObjectAnalytics/Device1Scenario1" in topic and "Interval" not in topic \
            and "Passthrough" not in topic:
        return {
            "type": "counter", "cam_uid": cam_uid, "ts": ts,
            "direction": "in", "scenario": "IN", "data": data, "raw": payload,
        }
    if "ObjectAnalytics/Device1Scenario2" in topic and "Interval" not in topic \
            and "Passthrough" not in topic:
        return {
            "type": "counter", "cam_uid": cam_uid, "ts": ts,
            "direction": "out", "scenario": "OUT", "data": data, "raw": payload,
        }

    # Motion (VMD or ONVIF MotionRegionDetector / MotionAlarm).
    if "/VMD/" in topic or "Motion" in topic:
        active_raw = data.get("active") or data.get("State") or data.get("state")
        return {
            "type": "motion", "cam_uid": cam_uid, "ts": ts,
            "direction": None, "scenario": None,
            "data": {"active": str(active_raw) in {"1", "true", "True"}, **data},
            "raw": payload,
        }

    # Image Health (tamper, blur, defocus, low light, etc.).
    if "ImageHealth" in topic:
        return {
            "type": "health", "cam_uid": cam_uid, "ts": ts,
            "direction": None, "scenario": None, "data": data, "raw": payload,
        }

    return None
```

- [ ] **Step 4: Chạy parser test — verify PASS**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai/services/event_collector && PYTHONPATH=src python3 -m pytest tests/test_parser.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Port `repo.py` (BỎ occupancy insert)**

Create `services/event_collector/src/event_collector/repo.py`. ⚠️ Khác DCNET: `ensure_cam` KHÔNG còn insert vào bảng `occupancy` (Phase 1 không có bảng đó; occupancy derived query-time):

```python
"""Persist parsed events to PostgreSQL (store-only; no occupancy table, no pg_notify)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg


class Repo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def cam_id_for(self, cam_uid: str) -> int | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT id FROM cameras WHERE cam_uid = $1", cam_uid)
            return int(row["id"]) if row else None

    async def ensure_cam(self, cam_uid: str, name: str, rtsp_url: str) -> int:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                """
                INSERT INTO cameras (cam_uid, name, rtsp_url)
                VALUES ($1, $2, $3)
                ON CONFLICT (cam_uid) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                cam_uid, name, rtsp_url,
            )
            return int(row["id"])

    async def insert_counter(
        self, *, cam_id: int, ts: datetime, direction: str,
        scenario: str, data: dict[str, Any], raw: dict[str, Any],
    ) -> int | None:
        """Insert one counter crossing row. Returns event_id, or None if duplicate.

        Counting is done at query time (COUNT of rows). We store the full raw
        envelope (incl. cumulative totalHuman) for audit only.
        """
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                """
                INSERT INTO events
                    (cam_id, ts, type, direction, axis_object_id, payload)
                VALUES ($1, $2, 'counter', $3, $4, $5)
                ON CONFLICT (cam_id, axis_object_id, ts, direction) DO NOTHING
                RETURNING id
                """,
                cam_id, ts, direction, scenario, json.dumps(raw),
            )
            return int(row["id"]) if row else None
```

- [ ] **Step 6: Port `main.py` (DSN ưu tiên DATABASE_URL + client_id default mới)**

Create `services/event_collector/src/event_collector/main.py`. ⚠️ Khác DCNET: `_dsn()` ưu tiên `DATABASE_URL`; `MQTT_CLIENT_ID` default = `event_collector_cameraai`:

```python
"""event_collector — MQTT → Postgres pipeline.

Subscribes to `axis/#` on the broker, parses each event, persists to DB.
Counting is done at query time (COUNT of rows) — no occupancy mutations here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

import aiomqtt
import asyncpg
import structlog

from event_collector.parser import parse_event
from event_collector.repo import Repo


def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()),
                        stream=sys.stdout)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
    )


log = structlog.get_logger("event_collector")


# Schema collector tự đảm bảo tồn tại trước khi consume — KHÔNG phụ thuộc thứ tự
# boot của FDW. Idempotent (CREATE TABLE IF NOT EXISTS); FDW init_db() cũng tạo
# cùng schema → dual-owner an toàn, ai boot trước cũng được.
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS cameras (
    id          SERIAL PRIMARY KEY,
    cam_uid     TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    rtsp_url    TEXT NOT NULL,
    mjpeg_url   TEXT,
    vendor      TEXT DEFAULT 'axis',
    model       TEXT,
    location    TEXT,
    enabled     BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    cam_id          INT REFERENCES cameras(id),
    ts              TIMESTAMPTZ NOT NULL,
    type            TEXT NOT NULL,
    direction       TEXT,
    axis_object_id  TEXT,
    payload         JSONB NOT NULL,
    snapshot_path   TEXT,
    face_path       TEXT,
    face_score      REAL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (cam_id, axis_object_id, ts, direction)
);
CREATE INDEX IF NOT EXISTS events_cam_ts ON events (cam_id, ts DESC);
CREATE INDEX IF NOT EXISTS events_type_ts ON events (type, ts DESC);
"""


async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as c:
        await c.execute(_SCHEMA_DDL)


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return (
        f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
        f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
    )


def _cam_name() -> str:
    return os.environ.get("CAM_NAME", "Cua chinh phong IT")


def _rtsp_url() -> str:
    ip = os.environ.get("CAM_IP", "192.168.100.47")
    return os.environ.get("CAM_RTSP_URL", f"rtsp://{ip}/axis-media/media.amp")


async def handle_message(topic: str, payload_raw: bytes, repo: Repo) -> None:
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        log.warning("bad_json", topic=topic,
                    sample=payload_raw[:120].decode(errors="replace"))
        return

    event = parse_event(topic, payload)
    if event is None:
        return

    cam_id = await repo.cam_id_for(event["cam_uid"])
    if cam_id is None:
        cam_id = await repo.ensure_cam(event["cam_uid"], _cam_name(), _rtsp_url())
        log.info("cam_auto_registered", cam_uid=event["cam_uid"], cam_id=cam_id)

    if event["type"] == "counter":
        ev_id = await repo.insert_counter(
            cam_id=cam_id, ts=event["ts"],
            direction=event["direction"], scenario=event["scenario"],
            data=event["data"], raw=event["raw"],
        )
        if ev_id is not None:
            log.info("counter_inserted", event_id=ev_id,
                     direction=event["direction"],
                     total_human=event["data"].get("totalHuman"))
    # motion + health: ignored in counting scope.


async def consume_loop(repo: Repo) -> None:
    host = os.environ["MQTT_HOST"]
    port = int(os.environ.get("MQTT_PORT", "1883"))
    user = os.environ.get("MQTT_USER") or None
    pwd = os.environ.get("MQTT_PASSWORD") or None
    topic_filter = f"{os.environ.get('MQTT_TOPIC_PREFIX', 'axis')}/#"
    # clientId UNIQUE per broker — collector DCNET prod cũng đọc cùng broker.
    client_id = os.environ.get("MQTT_CLIENT_ID", "event_collector_cameraai")
    tls = os.environ.get("MQTT_TLS", "false").lower() == "true"
    tls_params = aiomqtt.TLSParameters() if tls else None

    while True:
        try:
            async with aiomqtt.Client(
                hostname=host, port=port, username=user, password=pwd,
                identifier=client_id, tls_params=tls_params,
            ) as client:
                log.info("mqtt_connected", host=host, port=port,
                         client_id=client_id, tls=tls, topic_filter=topic_filter)
                await client.subscribe(topic_filter)
                async for msg in client.messages:
                    await handle_message(str(msg.topic), bytes(msg.payload), repo)
        except aiomqtt.MqttError as exc:
            log.warning("mqtt_disconnected", error=str(exc))
            await asyncio.sleep(2)


async def amain() -> None:
    _configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    pool: asyncpg.Pool = await asyncpg.create_pool(_dsn(), min_size=2, max_size=5)
    await ensure_schema(pool)   # tránh race: collector INSERT trước khi FDW tạo bảng
    repo = Repo(pool)
    log.info("event_collector_starting")
    try:
        await consume_loop(repo)
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Tạo requirements.txt + Dockerfile**

Create `services/event_collector/requirements.txt`:

```
aiomqtt==2.3.0
asyncpg==0.30.0
structlog==24.4.0
```

Create `services/event_collector/Dockerfile`:

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src /app/src

CMD ["python", "-m", "event_collector.main"]
```

- [ ] **Step 8: Thêm service vào `docker-compose.yml`**

Thêm block sau (cùng cấp indent với `fall_detection_web:`), trước khối `volumes:`:

```yaml
  event_collector:
    build: ./services/event_collector
    env_file:
      - path: ./services/event_collector/.env
        required: false
    environment:
      DATABASE_URL: postgresql://dcnet:${DB_PASSWORD:-dcnet_dev}@postgres:5432/dcnet
      MQTT_HOST: ${MQTT_HOST:-camera-test.dcnet.vn}
      MQTT_PORT: ${MQTT_PORT:-8883}
      MQTT_TLS: ${MQTT_TLS:-true}
      MQTT_USER: ${MQTT_USER:-}
      MQTT_PASSWORD: ${MQTT_PASSWORD:-}
      MQTT_CLIENT_ID: ${MQTT_CLIENT_ID:-event_collector_cameraai}
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped
```

- [ ] **Step 9: Build collector image — verify build OK**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai && docker compose build event_collector
```

Expected: build thành công, không lỗi pip/COPY.

- [ ] **Step 10: Commit**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
git add services/event_collector docker-compose.yml
git commit -m "feat(phase1): event_collector service (MQTT->Postgres store-only) + parser test"
```

---

## Task 4: UI — route `/counting` + `/api/counting` + template + nav link

Route render trang "Đếm ra/vào" (3 số + chart giờ + log) + API JSON cho polling auto-refresh. ⚠️ Route phải khai báo TRƯỚC catch-all `/{page_name}`.

**Files:**
- Modify: `fall_detection_web/app.py` (thêm route trước app.py:152; import `db.counting`/`counting`)
- Create: `fall_detection_web/templates/counting.html`
- Modify: `fall_detection_web/templates/index.html` (nav link)

**Interfaces:**
- Consumes: `counting.bucket_hourly`/`summarize` (Task 2); `db.counting_occupancy_today`/`db.counting_crossings`/`db.list_cameras` (Task 1); `auth.require_auth`, `templates` (app.py).
- Produces: `GET /counting` (HTML), `GET /api/counting` (JSON `{occupancy, in, out, hourly: [{hour,in,out}...], log: [{ts,direction}...]}`).

- [ ] **Step 1: Thêm route `/counting` + `/api/counting` vào app.py**

Trong `fall_detection_web/app.py`, thêm import gần đầu (cạnh `import db`):

```python
import counting
```

Thêm 2 route NGAY TRƯỚC `@app.get("/{page_name}")` (app.py:152). API trả VN-today bucket:

```python
@app.get("/counting", response_class=HTMLResponse)
def counting_page(request: Request, _: str = Depends(auth.require_auth)):
    return templates.TemplateResponse(request=request, name="counting.html", context={})


@app.get("/api/counting")
def api_counting(_: str = Depends(auth.require_auth)):
    from datetime import datetime, timezone, timedelta
    vn_today = datetime.now(timezone(timedelta(hours=7))).date()
    occ = db.counting_occupancy_today()
    crossings = db.counting_crossings(vn_today)
    hourly = counting.bucket_hourly(crossings, vn_today)
    log_rows = [
        {"ts": c["ts"].astimezone(timezone(timedelta(hours=7))).strftime("%H:%M:%S"),
         "direction": c["direction"]}
        for c in crossings[:50]
    ]
    return {
        "occupancy": occ["occupancy"], "in": occ["in"], "out": occ["out"],
        "hourly": hourly, "log": log_rows,
    }
```

- [ ] **Step 2: Tạo template `counting.html`**

Create `fall_detection_web/templates/counting.html` (self-contained, polling 3s, vanilla JS — không phụ thuộc thư viện ngoài; chart = bar div đơn giản):

```html
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Đếm ra/vào — DCNET Camera</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.ico">
  <style>
    :root { --bg:#0f172a; --card:#1e293b; --text:#e2e8f0; --muted:#94a3b8;
            --in:#22c55e; --out:#ef4444; --acc:#38bdf8; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text);
           font-family:system-ui,-apple-system,sans-serif; padding:24px; }
    a { color:var(--acc); text-decoration:none; }
    h1 { font-size:1.4rem; margin:0 0 16px; }
    .nums { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin-bottom:24px; }
    .num { background:var(--card); border-radius:12px; padding:20px; text-align:center; }
    .num .v { font-size:2.4rem; font-weight:700; }
    .num .l { color:var(--muted); font-size:.85rem; margin-top:4px; }
    .num.in .v { color:var(--in); } .num.out .v { color:var(--out); }
    .grid2 { display:grid; grid-template-columns:2fr 1fr; gap:16px; }
    .panel { background:var(--card); border-radius:12px; padding:16px; }
    .panel h2 { font-size:1rem; margin:0 0 12px; color:var(--muted); }
    .bars { display:flex; align-items:flex-end; gap:3px; height:180px; }
    .bar { flex:1; display:flex; flex-direction:column; justify-content:flex-end;
           align-items:center; gap:1px; }
    .bar .seg-in { width:100%; background:var(--in); border-radius:2px 2px 0 0; }
    .bar .seg-out { width:100%; background:var(--out); }
    .bar .hr { font-size:.6rem; color:var(--muted); margin-top:4px; }
    .log { max-height:360px; overflow-y:auto; font-size:.85rem; }
    .log .row { display:flex; justify-content:space-between; padding:6px 0;
                border-bottom:1px solid rgba(148,163,184,.15); }
    .tag { padding:1px 8px; border-radius:6px; font-weight:600; font-size:.75rem; }
    .tag.in { background:rgba(34,197,94,.2); color:var(--in); }
    .tag.out { background:rgba(239,68,68,.2); color:var(--out); }
    @media (max-width:760px){ .grid2{grid-template-columns:1fr;} }
  </style>
</head>
<body>
  <p><a href="/">← Dashboard</a></p>
  <h1>Đếm người ra/vào — hôm nay</h1>
  <div class="nums">
    <div class="num"><div class="v" id="occ">–</div><div class="l">Đang trong phòng</div></div>
    <div class="num in"><div class="v" id="in">–</div><div class="l">VÀO hôm nay</div></div>
    <div class="num out"><div class="v" id="out">–</div><div class="l">RA hôm nay</div></div>
  </div>
  <div class="grid2">
    <div class="panel">
      <h2>Theo giờ (VN) — <span style="color:var(--in)">vào</span> / <span style="color:var(--out)">ra</span></h2>
      <div class="bars" id="bars"></div>
    </div>
    <div class="panel">
      <h2>Log crossing gần nhất</h2>
      <div class="log" id="log"></div>
    </div>
  </div>
  <script>
    async function refresh() {
      try {
        const r = await fetch("/api/counting", { credentials: "same-origin" });
        if (!r.ok) return;
        const d = await r.json();
        document.getElementById("occ").textContent = d.occupancy;
        document.getElementById("in").textContent = d.in;
        document.getElementById("out").textContent = d.out;
        const max = Math.max(1, ...d.hourly.map(h => h.in + h.out));
        document.getElementById("bars").innerHTML = d.hourly.map(h => `
          <div class="bar" title="${h.hour}h — vào ${h.in}, ra ${h.out}">
            <div class="seg-in" style="height:${h.in / max * 160}px"></div>
            <div class="seg-out" style="height:${h.out / max * 160}px"></div>
            <div class="hr">${h.hour}</div>
          </div>`).join("");
        document.getElementById("log").innerHTML = d.log.length
          ? d.log.map(l => `<div class="row"><span>${l.ts}</span>
              <span class="tag ${l.direction}">${l.direction === "in" ? "VÀO" : "RA"}</span></div>`).join("")
          : '<div style="color:var(--muted)">Chưa có crossing hôm nay.</div>';
      } catch (e) { /* giữ giá trị cũ khi lỗi mạng */ }
    }
    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
```

- [ ] **Step 3: Thêm nav link vào index.html**

Trong `fall_detection_web/templates/index.html`, nav block (~line 623-630), thêm sau link Cameras (line 625):

```html
        <a class="nav-btn" href="/counting"><span data-icon="bar-chart-2"></span>Đếm ra/vào</a>
```

- [ ] **Step 4: Chạy app + verify trang render (login → /counting)**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai && docker compose up -d --build fall_detection_web
```

Sau đó verify:
- `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8090/counting` → `302` hoặc `401` (chưa login — auth chặn, đúng).
- Login qua browser `http://localhost:8090/login` → vào `http://localhost:8090/counting` → thấy 3 số (0/0/0 nếu chưa data), chart 24 cột, log "Chưa có crossing hôm nay."
- `curl` API với cookie sau login (hoặc DevTools Network) → `/api/counting` trả JSON `{occupancy, in, out, hourly: [24], log: []}`.

Expected: trang render, không 500. Nav có link "Đếm ra/vào".

- [ ] **Step 5: Commit**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
git add fall_detection_web/app.py fall_detection_web/templates/counting.html fall_detection_web/templates/index.html
git commit -m "feat(phase1): UI /counting + /api/counting + nav link"
```

---

## Task 5: Live verify end-to-end + docs

Collector nối cloud broker đọc crossing thật → `events` có row → trang /counting hiện đúng. Cập nhật CLAUDE.md đánh dấu Phase 1 DONE.

**Files:**
- Modify: `CLAUDE.md` (bảng phase: Phase 1 → DONE)
- Reference: `docs/superpowers/specs/2026-06-26-phase1-counting-design.md` (cập nhật Trạng thái)

- [ ] **Step 1: Up full stack với cred broker thật**

Cần `MQTT_PASSWORD` (cloud broker). Set `.env` ở root camera-ai (gitignored):

```bash
# /Users/vovanduc/Code/dcnet/camera-ai/.env
DB_PASSWORD=dcnet_dev
MQTT_HOST=camera-test.dcnet.vn
MQTT_PORT=8883
MQTT_TLS=true
MQTT_USER=<user broker>
MQTT_PASSWORD=<password broker thật>
MQTT_CLIENT_ID=event_collector_cameraai
```

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai && docker compose up -d --build
docker compose logs -f event_collector
```

Expected log: `mqtt_connected` với `client_id=event_collector_cameraai tls=true`. Khi có người qua cửa (hoặc cam có traffic): `counter_inserted` với direction in/out.

- [ ] **Step 2: psql đối chiếu COUNT**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
docker compose exec -T postgres psql -U dcnet -d dcnet -c \
  "SELECT direction, COUNT(*) FROM events WHERE type='counter'
   AND (ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date = (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
   GROUP BY direction;"
```

Expected: rows `in`/`out` với count > 0 (nếu cam có traffic). Số khớp với 3 số trên trang /counting.

- [ ] **Step 3: Verify trang /counting khớp DB**

Browser → `/counting`: occupancy = max(0, IN−OUT), IN/OUT khớp psql, chart có cột ở giờ có event, log hiện crossing gần nhất (giờ VN). Đối chiếu thủ công 1 lần.

- [ ] **Step 4: Cập nhật CLAUDE.md + spec status**

Trong `CLAUDE.md` (camera-ai), bảng phase migration: Phase 1 → ✅ DONE + plan link. Trong spec `2026-06-26-phase1-counting-design.md` dòng `**Trạng thái:**` → `DONE (implemented)`.

- [ ] **Step 5: Commit**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
git add CLAUDE.md docs/superpowers/specs/2026-06-26-phase1-counting-design.md
git commit -m "docs(phase1): mark counting module DONE + live-verified"
```

---

## Self-Review (đã chạy)

**Spec coverage:**
- §4 Schema (cameras+events+seed+index) → Task 1 ✅
- §5a event_collector (parser/repo/main/Dockerfile/req/compose) → Task 3 ✅
- §5b counting.py port → Task 2 ✅
- §5c db.py counting queries (occupancy/crossings/list_cameras/cam_id_for) → Task 1 ✅
- §5d UI route /counting + /api/counting + template + nav → Task 4 ✅
- §5e compose service → Task 3 Step 8 ✅
- §7 Testing (counting unit, parser unit, live verify, psql) → Task 2/3/5 ✅
- §9 Open questions: occupancy clamp ≥0 (Task 1 `counting_occupancy_today`); seed SQL idempotent trong init (Task 1) — cả 2 theo đề xuất review ✅

**Adaptations vs DCNET (đã ghi rõ trong task):**
- `repo.ensure_cam` BỎ insert `occupancy` (Task 3 Step 5) — Phase 1 không có bảng occupancy.
- `main._dsn()` ưu tiên `DATABASE_URL` (Task 3 Step 6) — compose camera-ai dùng env này.
- `MQTT_CLIENT_ID` default → `event_collector_cameraai` (Task 3 Step 6) — không kick collector DCNET prod.
- **Collector `ensure_schema()` on boot** (Task 3 Step 6) — collector + FDW boot concurrent (chỉ depends_on postgres), collector có thể INSERT crossing TRƯỚC khi FDW `init_db()` tạo bảng → `UndefinedTableError` crash. Collector tự chạy `CREATE TABLE IF NOT EXISTS` (idempotent, dual-owner với FDW init_db) trước consume. Seed cam vẫn ở FDW init_db; nếu chưa seed, collector auto-register cam qua `ensure_cam` ở event đầu.
- Route `/counting` trước catch-all `/{page_name}` (Task 4 Step 1) — FastAPI match theo thứ tự.
- Test import dùng `from counting import ...` / `from event_collector.parser import ...` (chạy từ thư mục tương ứng, PYTHONPATH=src cho collector).

**Placeholder scan:** không có TODO/TBD; mọi step có code/lệnh cụ thể + expected output.

**Type consistency:** `counting_crossings` trả `{ts, direction}` ↔ `bucket_hourly` consume `c["ts"]`/`c["direction"]` ✅. `counting_occupancy_today` trả `{in,out,occupancy}` ↔ API map đúng ✅.

---
## Liên quan
- Spec: [phase1-counting-design](../specs/2026-06-26-phase1-counting-design.md) · Tổng thể: [migration design](../specs/2026-06-26-dcnet-platform-migration-design.md)
- Trước: Phase 0 (merged) · Sau: [Phase 2 Group/Re-ID](../specs/2026-06-26-phase2-group-reid-design.md)
