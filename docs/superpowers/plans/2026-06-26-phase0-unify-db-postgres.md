# Phase 0 — Unify DB (SQLite → Postgres) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chuyển tầng lưu trữ của `fall_detection_web` (FDW) từ SQLite sang PostgreSQL mới (dựng trong camera-ai), hành vi người dùng không đổi — nền cho việc gộp counting/Re-ID ở phase sau.

**Architecture:** Rewrite `fall_detection_web/db.py` từ `sqlite3` (sync) → `psycopg` v3 (sync, connection pool), **giữ nguyên signature mọi hàm public** nên `app.py`/`monitor.py`/`config.py`/`redis_cache.py` không phải sửa. Đổi tên bảng `events`→`incidents` (tránh va chạm bảng `events` của counting DCNET ở Phase 1). Postgres chạy như service mới trong `docker-compose.yml`. Greenfield — không migrate data SQLite cũ.

**Tech Stack:** Python 3.12, FastAPI (sync/threaded), `psycopg[binary]` v3 + `psycopg_pool`, PostgreSQL 16 (`pgvector/pgvector:pg16` — chuẩn bị sẵn cho Re-ID Phase 2), Docker Compose. FDW **không có test suite** — verify bằng smoke script + chạy app (chuẩn repo).

## Global Constraints

- Ngôn ngữ doc/commit/user-facing: **tiếng Việt + tech term English**, UTF-8 (theo `camera-ai/CLAUDE.md`).
- **KHÔNG bump `simple_ai_vision/config.yaml` version** — Phase 0 chỉ chạm `fall_detection_web/` (theo AGENTS.md: sửa FDW không bump add-on version).
- FDW giữ model **sync/threaded** — KHÔNG async hoá. Shared state qua `threading.Lock` (không đụng trong Phase 0).
- **Giữ nguyên signature mọi hàm public của `db.py`** (init_db, get_conn, now_iso, local_iso, insert_event, update_event_teldrive_image, update_event_image, find_matching_teldrive_image, get_events, get_recordings, get_recordings_total, get_uploaded_video_records, get_events_total, count_events, clear_events, delete_old_events, get_incident_trends, get_user, create_user, update_user, list_users, get_setting, set_setting, set_settings_bulk, get_all_settings, delete_setting, save_event_image, cleanup_event_images, invalidate_event_caches) + hằng `EVENT_IMAGES_DIR`, `DATA_DIR`, `LOCAL_TZ`.
- Bảng đổi tên: **`events`→`incidents`** (trong DB + mọi SQL của db.py). Tên cột giữ y nguyên.
- DSN từ env: `DATABASE_URL` (ưu tiên) hoặc `DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME`.
- Greenfield: **KHÔNG** port data SQLite. Bỏ `_migrate_jsonl` (legacy sqlite-era).
- Commit sau mỗi task (theo AGENTS.md: commit sau mỗi thay đổi).

---

### Task 1: Hạ tầng — deps + Postgres compose + DSN env

**Files:**
- Modify: `fall_detection_web/requirements.txt`
- Create: `docker-compose.yml` (repo root camera-ai)
- Create: `fall_detection_web/.env.example`

**Interfaces:**
- Produces: service `postgres` (host `postgres`, port 5432, db `dcnet`, user `dcnet`); env `DATABASE_URL` cho FDW. Task 2 dùng DSN này.

- [ ] **Step 1: Thêm psycopg vào requirements**

Sửa `fall_detection_web/requirements.txt`, thêm 2 dòng (sau `redis`):

```
psycopg[binary]==3.2.3
psycopg-pool==3.2.3
```

- [ ] **Step 2: Tạo docker-compose.yml ở root camera-ai**

Create `docker-compose.yml`:

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: dcnet
      POSTGRES_PASSWORD: ${DB_PASSWORD:-dcnet_dev}
      POSTGRES_DB: dcnet
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dcnet -d dcnet"]
      interval: 5s
      timeout: 5s
      retries: 10

  fall_detection_web:
    build: ./fall_detection_web
    env_file: ./fall_detection_web/.env
    environment:
      DATABASE_URL: postgresql://dcnet:${DB_PASSWORD:-dcnet_dev}@postgres:5432/dcnet
    volumes:
      - fdw_data:/app/data
    ports:
      - "8090:8090"
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped

volumes:
  pgdata:
  fdw_data:
```

> Lưu ý: `fall_detection_web/Dockerfile` đã tồn tại (CMD `uvicorn app:app --host 0.0.0.0 --port 8090`). Nếu requirements pin `torch==2.5.1+cpu` build fail trên arm64, dùng index mặc định (`torch==2.5.1`) — ngoài scope DB nhưng ghi chú cho người chạy.

- [ ] **Step 3: Tạo .env.example với DSN**

Create `fall_detection_web/.env.example`:

```
# DB (Phase 0 — Postgres). Ưu tiên DATABASE_URL; hoặc set DB_* rời.
DATABASE_URL=postgresql://dcnet:dcnet_dev@localhost:5432/dcnet
# DB_HOST=localhost
# DB_PORT=5432
# DB_USER=dcnet
# DB_PASSWORD=dcnet_dev
# DB_NAME=dcnet

# AI Vision (OpenAI-compatible) — để trống nếu chưa dùng
# AI_BASE_URL=
# AI_API_KEY=
# VISION_MODEL=
```

- [ ] **Step 4: Dựng Postgres + verify reachable**

Run:
```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
docker compose up -d postgres
docker compose exec -T postgres psql -U dcnet -d dcnet -c "SELECT version();"
```
Expected: in ra dòng `PostgreSQL 16.x ...` (Postgres sống).

- [ ] **Step 5: Commit**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
git add docker-compose.yml fall_detection_web/requirements.txt fall_detection_web/.env.example
git commit -m "feat(phase0): hạ tầng Postgres compose + psycopg deps + DSN env"
```

---

### Task 2: Rewrite `db.py` sqlite3 → psycopg (toàn bộ file)

**Files:**
- Modify: `fall_detection_web/db.py` (rewrite toàn bộ phần SQL; giữ signature + phần ảnh/cv2)
- Create: `fall_detection_web/smoke_db.py` (smoke script thay test suite)

**Interfaces:**
- Consumes: `DATABASE_URL` env từ Task 1.
- Produces: `db.py` API y hệt cũ nhưng chạy trên Postgres, bảng `incidents/users/settings`. `app.py`/`monitor.py`/`config.py` KHÔNG đổi.

- [ ] **Step 1: Viết smoke script (đóng vai test)**

Create `fall_detection_web/smoke_db.py`:

```python
"""Smoke test db.py trên Postgres (FDW không có pytest). Chạy: python smoke_db.py
Yêu cầu: DATABASE_URL trỏ Postgres sạch."""
import db

def main():
    db.init_db()
    # settings
    db.set_setting("k1", "v1")
    assert db.get_setting("k1") == "v1", "get_setting fail"
    db.set_settings_bulk({"k2": "v2", "k3": "v3"})
    alls = db.get_all_settings()
    assert alls.get("k2") == "v2" and alls.get("k3") == "v3", "bulk fail"
    db.delete_setting("k1")
    assert db.get_setting("k1", "MISSING") == "MISSING", "delete_setting fail"
    # users
    db.create_user("admin", "hash123")
    u = db.get_user("admin")
    assert u and u["username"] == "admin" and u["password_hash"] == "hash123", "user fail"
    assert any(x["username"] == "admin" for x in db.list_users()), "list_users fail"
    # incidents (insert without image)
    r = db.insert_event("verified", image_path=None, save_image=False,
                        camera="cam1", confidence=0.9, ai_result="EMERGENCY",
                        message="smoke")
    assert isinstance(r["id"], int) and r["id"] > 0, "insert_event id fail"
    evs = db.get_events(limit=10)
    assert any(e["id"] == r["id"] and e["camera"] == "cam1" for e in evs), "get_events fail"
    assert db.count_events() >= 1, "count_events fail"
    assert db.get_events_total(ai_result="EMERGENCY") >= 1, "events_total fail"
    trends = db.get_incident_trends(7)
    assert isinstance(trends, list), "trends fail"
    print("SMOKE OK: settings/users/incidents CRUD trên Postgres pass")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Chạy smoke để xác nhận FAIL (chưa rewrite)**

Run:
```bash
cd /Users/vovanduc/Code/dcnet/camera-ai/fall_detection_web
DATABASE_URL=postgresql://dcnet:dcnet_dev@localhost:5432/dcnet python smoke_db.py
```
Expected: FAIL — `db.py` còn dùng sqlite3 (kết nối file `data/fall_detection.db`, bảng `events` không phải `incidents`, hoặc lỗi do schema chưa ở Postgres). Đây là trạng thái "test đỏ".

- [ ] **Step 3: Rewrite toàn bộ `db.py`**

Thay TOÀN BỘ nội dung `fall_detection_web/db.py` bằng:

```python
"""Database layer (PostgreSQL via psycopg) — incidents, users, settings.

Phase 0: chuyển từ SQLite sang Postgres. Giữ nguyên signature hàm public.
Bảng 'events' cũ → 'incidents' (tránh va chạm bảng counting 'events' ở phase sau).
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
EVENT_IMAGES_DIR = DATA_DIR / "event_images"

LOCAL_TZ = timezone(timedelta(hours=7))
MAX_EVENTS = 5000
PRUNE_BATCH = 500
IMAGE_MAX_AGE_SECONDS = 86400  # 24h

logger = logging.getLogger("fall_detection_web")


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return (
        f"postgresql://{os.environ.get('DB_USER', 'dcnet')}:"
        f"{os.environ.get('DB_PASSWORD', 'dcnet_dev')}@"
        f"{os.environ.get('DB_HOST', 'localhost')}:"
        f"{os.environ.get('DB_PORT', '5432')}/"
        f"{os.environ.get('DB_NAME', 'dcnet')}"
    )


_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=_dsn(),
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EVENT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn() -> Generator[psycopg.Connection, None, None]:
    """Mượn connection từ pool. Tự commit khi thoát block, rollback nếu lỗi."""
    ensure_data_dir()
    with _get_pool().connection() as conn:
        yield conn


def init_db() -> None:
    """Tạo bảng nếu chưa có (Postgres, schema tường minh — không cần _ensure_column)."""
    ensure_data_dir()
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id           BIGSERIAL PRIMARY KEY,
                time         TEXT NOT NULL,
                time_local   TEXT,
                status       TEXT NOT NULL,
                camera       TEXT,
                confidence   REAL,
                ai_result    TEXT,
                ai_raw       TEXT,
                ai_response  TEXT,
                message      TEXT,
                error        TEXT,
                image_file   TEXT,
                teldrive_image_id   TEXT,
                teldrive_image_name TEXT,
                teldrive_image_path TEXT,
                teldrive_video_id   TEXT,
                teldrive_video_name TEXT,
                teldrive_video_path TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_time ON incidents (time DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            BIGSERIAL PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def cleanup_event_images() -> None:
    ensure_data_dir()
    cutoff = time.time() - IMAGE_MAX_AGE_SECONDS
    for path in EVENT_IMAGES_DIR.glob("*.jpg"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def save_event_image(source_path: Path | None, status_name: str) -> str:
    if not source_path or not source_path.exists():
        return ""
    cleanup_event_images()
    if random.random() < 0.05:
        delete_old_events(7)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    safe_status = "".join(ch for ch in status_name if ch.isalnum() or ch in ("_", "-")) or "event"
    target = EVENT_IMAGES_DIR / f"{stamp}_{safe_status}.jpg"

    try:
        import cv2
        img = cv2.imread(str(source_path))
        if img is not None:
            h, w = img.shape[:2]
            if w > 1280:
                new_w = 1280
                new_h = int(h * (1280 / w))
                img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(target), img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        else:
            shutil.copyfile(source_path, target)
    except Exception as e:
        logger.warning(f"[DB] Lỗi nén ảnh {source_path}, copy raw: {e}")
        shutil.copyfile(source_path, target)

    return target.name


def _prune_events(conn: psycopg.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"]
    if count > MAX_EVENTS:
        to_delete = count - MAX_EVENTS + PRUNE_BATCH
        rows = conn.execute(
            "SELECT image_file FROM incidents ORDER BY id ASC LIMIT %s",
            (to_delete,),
        ).fetchall()
        for row in rows:
            img = str(row["image_file"] or "").strip()
            if img:
                try:
                    (EVENT_IMAGES_DIR / img).unlink()
                except OSError:
                    pass
        conn.execute(
            "DELETE FROM incidents WHERE id IN (SELECT id FROM incidents ORDER BY id ASC LIMIT %s)",
            (to_delete,),
        )
        logger.info("[DB] Pruned %d old incidents", to_delete)


def invalidate_event_caches() -> None:
    try:
        import config as _config
        import redis_cache as _redis_cache
        c = _config.read_config()
        _redis_cache.clear_cache_pattern("events:list:*", c)
        _redis_cache.clear_cache_pattern("recordings:list:*", c)
        _redis_cache.delete_cache("events:trends", c)
    except Exception as exc:
        logger.warning("[DB] Failed to invalidate Redis caches: %s", exc)


def insert_event(status_name: str, image_path: Path | None = None, save_image: bool = True, **fields: Any) -> dict[str, Any]:
    image_file = save_event_image(image_path, status_name) if save_image else ""
    t = str(fields.get("event_time") or now_iso())
    t_local = str(fields.get("event_time_local") or local_iso())
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO incidents (time,time_local,status,camera,confidence,ai_result,ai_raw,ai_response,message,error,image_file,teldrive_image_id,teldrive_image_name,teldrive_image_path,teldrive_video_id,teldrive_video_name,teldrive_video_path) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                t, t_local, status_name,
                fields.get("camera", ""),
                fields.get("confidence"),
                fields.get("ai_result", ""),
                fields.get("ai_raw", ""),
                fields.get("ai_response", ""),
                fields.get("message", ""),
                fields.get("error", ""),
                image_file,
                fields.get("teldrive_image_id", ""),
                fields.get("teldrive_image_name", ""),
                fields.get("teldrive_image_path", ""),
                fields.get("teldrive_video_id", ""),
                fields.get("teldrive_video_name", ""),
                fields.get("teldrive_video_path", ""),
            ),
        ).fetchone()
        event_id = row["id"]
        _prune_events(conn)
    invalidate_event_caches()
    return {"id": event_id, "image_file": image_file}


def update_event_teldrive_image(event_id: int, file_data: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE incidents SET teldrive_image_id=%s, teldrive_image_name=%s, teldrive_image_path=%s WHERE id=%s",
            (
                str(file_data.get("id", "")),
                str(file_data.get("name", "")),
                str(file_data.get("path", "")),
                event_id,
            ),
        )
    invalidate_event_caches()


def update_event_image(event_id: int, image_path: Path, status_name: str = "recording_thumb") -> str:
    image_file = save_event_image(image_path, status_name)
    if not image_file:
        return ""
    with get_conn() as conn:
        conn.execute("UPDATE incidents SET image_file=%s WHERE id=%s", (image_file, event_id))
    invalidate_event_caches()
    return image_file


def find_matching_teldrive_image(conn: psycopg.Connection, camera: str, event_time_str: str) -> tuple[str, str] | None:
    if not camera or not event_time_str:
        return None
    try:
        t_event = datetime.fromisoformat(event_time_str)
    except Exception:
        return None
    t_min = (t_event - timedelta(seconds=120)).isoformat(timespec="seconds")
    t_max = (t_event + timedelta(seconds=120)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT teldrive_image_id, teldrive_image_name FROM incidents "
        "WHERE camera = %s AND status = 'teldrive_video_uploaded' "
        "AND teldrive_image_id IS NOT NULL AND teldrive_image_id != '' "
        "AND time >= %s AND time <= %s LIMIT 1",
        (camera, t_min, t_max),
    ).fetchone()
    if row:
        return row["teldrive_image_id"], row["teldrive_image_name"]
    return None


def get_events(
    limit: int = 100,
    offset: int = 0,
    ai_result: str | None = None,
    camera: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM incidents"
    params: list[Any] = []
    conditions = []
    if ai_result:
        conditions.append("ai_result = %s")
        params.append(ai_result)
    if camera:
        conditions.append("camera = %s")
        params.append(camera)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY time DESC, id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            image_file = str(event.get("image_file") or "").strip()
            if event.get("teldrive_image_id") and event.get("teldrive_image_name"):
                name = quote(str(event["teldrive_image_name"]), safe="")
                event["image_url"] = f"/api/teldrive/file/{event['teldrive_image_id']}/{name}"
            elif image_file and (EVENT_IMAGES_DIR / image_file).exists():
                event["image_url"] = f"/api/event-image/{image_file}"
            if not event.get("image_url") and event.get("status") == "verified":
                matching = find_matching_teldrive_image(conn, event.get("camera"), event.get("time"))
                if matching:
                    t_id, t_name = matching
                    name = quote(str(t_name), safe="")
                    event["image_url"] = f"/api/teldrive/file/{t_id}/{name}"
            events.append(event)
    return events


def get_recordings(
    limit: int = 100,
    offset: int = 0,
    camera: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM incidents WHERE teldrive_video_id IS NOT NULL AND teldrive_video_id != ''"
    params: list[Any] = []
    if camera:
        query += " AND camera = %s"
        params.append(camera)
    if date_from:
        query += " AND time >= %s"
        params.append(date_from)
    if date_to:
        query += " AND time <= %s"
        params.append(date_to)
    query += " ORDER BY time DESC, id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    recordings = [dict(row) for row in rows]
    for item in recordings:
        name = quote(str(item["teldrive_video_name"]), safe="")
        item["video_url"] = f"/api/teldrive/file/{item['teldrive_video_id']}/{name}"
        image_file = str(item.get("image_file") or "").strip()
        if item.get("teldrive_image_id") and item.get("teldrive_image_name"):
            img_name = quote(str(item["teldrive_image_name"]), safe="")
            item["image_url"] = f"/api/teldrive/file/{item['teldrive_image_id']}/{img_name}"
        elif image_file and (EVENT_IMAGES_DIR / image_file).exists():
            item["image_url"] = f"/api/event-image/{image_file}"
    return recordings


def get_recordings_total(camera: str | None = None, date_from: str | None = None, date_to: str | None = None) -> int:
    query = "SELECT COUNT(*) AS n FROM incidents WHERE teldrive_video_id IS NOT NULL AND teldrive_video_id != ''"
    params: list[Any] = []
    if camera:
        query += " AND camera = %s"
        params.append(camera)
    if date_from:
        query += " AND time >= %s"
        params.append(date_from)
    if date_to:
        query += " AND time <= %s"
        params.append(date_to)
    with get_conn() as conn:
        return conn.execute(query, params).fetchone()["n"]


def get_uploaded_video_records() -> list[dict[str, str]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, camera, message, image_file, teldrive_image_id, teldrive_video_name FROM incidents "
            "WHERE teldrive_video_id IS NOT NULL AND teldrive_video_id != ''"
        ).fetchall()
    return [
        {
            "id": str(row["id"] or ""),
            "camera": str(row["camera"] or ""),
            "message": str(row["message"] or ""),
            "image_file": str(row["image_file"] or ""),
            "teldrive_image_id": str(row["teldrive_image_id"] or ""),
            "teldrive_video_name": str(row["teldrive_video_name"] or ""),
        }
        for row in rows
    ]


def get_events_total(ai_result: str | None = None, camera: str | None = None, status: str | None = None) -> int:
    query = "SELECT COUNT(*) AS n FROM incidents"
    params: list[Any] = []
    conditions = []
    if ai_result:
        conditions.append("ai_result = %s")
        params.append(ai_result)
    if camera:
        conditions.append("camera = %s")
        params.append(camera)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    with get_conn() as conn:
        return conn.execute(query, params).fetchone()["n"]


def count_events() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"]


def clear_events(camera: str | None = None, recordings_only: bool = False, exclude_recordings: bool = False) -> int:
    conditions = []
    params: list[Any] = []
    if camera:
        conditions.append("camera = %s")
        params.append(camera)
    if recordings_only:
        conditions.append("teldrive_video_id IS NOT NULL AND teldrive_video_id != ''")
    if exclude_recordings:
        conditions.append("(teldrive_video_id IS NULL OR teldrive_video_id = '')")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    with get_conn() as conn:
        rows = conn.execute(f"SELECT image_file FROM incidents{where}", params).fetchall()
        cur = conn.execute(f"DELETE FROM incidents{where}", params)
        deleted = cur.rowcount
        remaining_images = {
            str(r["image_file"] or "").strip()
            for r in conn.execute("SELECT image_file FROM incidents WHERE image_file IS NOT NULL AND image_file != ''").fetchall()
        }

    for row in rows:
        image_file = str(row["image_file"] or "").strip()
        if not image_file or image_file in remaining_images:
            continue
        try:
            (EVENT_IMAGES_DIR / image_file).unlink()
        except OSError:
            pass
    invalidate_event_caches()
    return deleted


def delete_old_events(days: int = 7) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with get_conn() as conn:
        rows = conn.execute("SELECT image_file FROM incidents WHERE time < %s", (cutoff,)).fetchall()
        for row in rows:
            img = str(row["image_file"] or "").strip()
            if img:
                try:
                    (EVENT_IMAGES_DIR / img).unlink()
                except OSError:
                    pass
        deleted = conn.execute("DELETE FROM incidents WHERE time < %s", (cutoff,)).rowcount

    try:
        cache_dir = DATA_DIR / "teldrive_cache"
        if cache_dir.exists():
            cutoff_epoch = time.time() - (days * 86400)
            for path in cache_dir.iterdir():
                if path.is_file() and path.stat().st_mtime < cutoff_epoch:
                    try:
                        path.unlink()
                    except OSError:
                        pass
    except Exception as e:
        logger.warning(f"[DB] Lỗi dọn teldrive_cache: {e}")

    if deleted > 0:
        logger.info("[DB] Deleted %d incidents older than %d days", deleted, days)
    invalidate_event_caches()
    return deleted


def get_incident_trends(days: int = 7) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    query = """
        SELECT
            substr(coalesce(time_local, time), 1, 10) AS date_str,
            upper(coalesce(ai_result, status)) AS result,
            COUNT(*) AS count
        FROM incidents
        WHERE time >= %s
        GROUP BY date_str, result
    """
    with get_conn() as conn:
        rows = conn.execute(query, (cutoff,)).fetchall()
    return [dict(row) for row in rows]


# ── Users ──

def get_user(username: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=%s", (username,)).fetchone()
    return dict(row) if row else None


def create_user(username: str, password_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (%s,%s,%s)",
            (username, password_hash, now_iso()),
        )


def update_user(old_username: str, new_username: str, password_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET username=%s, password_hash=%s WHERE username=%s",
            (new_username, password_hash, old_username),
        )


def list_users() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, username, created_at FROM users").fetchall()
    return [dict(r) for r in rows]


# ── Settings ──

def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (%s,%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_iso()),
        )


def set_settings_bulk(data: dict[str, str]) -> None:
    ts = now_iso()
    rows = [(k, v, ts) for k, v in data.items()]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO settings (key, value, updated_at) VALUES (%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                rows,
            )


def get_all_settings() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def delete_setting(key: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key=%s", (key,))
```

- [ ] **Step 4: Chạy smoke để xác nhận PASS**

Run:
```bash
cd /Users/vovanduc/Code/dcnet/camera-ai/fall_detection_web
docker compose -f ../docker-compose.yml exec -T postgres psql -U dcnet -d dcnet -c "DROP TABLE IF EXISTS incidents, users, settings CASCADE;"
DATABASE_URL=postgresql://dcnet:dcnet_dev@localhost:5432/dcnet python smoke_db.py
```
Expected: `SMOKE OK: settings/users/incidents CRUD trên Postgres pass`
(Cần `pip install 'psycopg[binary]' psycopg-pool` trong venv chạy smoke, hoặc chạy trong container.)

- [ ] **Step 5: Verify bảng trên Postgres**

Run:
```bash
docker compose exec -T postgres psql -U dcnet -d dcnet -c "\dt"
```
Expected: liệt kê 3 bảng `incidents`, `users`, `settings` (KHÔNG có `events`).

- [ ] **Step 6: Commit**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
git add fall_detection_web/db.py fall_detection_web/smoke_db.py
git commit -m "feat(phase0): rewrite db.py sqlite3 -> psycopg (incidents/users/settings)"
```

---

### Task 3: Integration — chạy app end-to-end trên Postgres + cập nhật docs

**Files:**
- Modify: `camera-ai/CLAUDE.md` (ghi chú DB = Postgres, bảng `incidents`)
- Modify: `fall_detection_web/AGENTS.md` (đổi "SQLite" → "Postgres (psycopg)")

**Interfaces:**
- Consumes: `db.py` (Task 2), compose (Task 1).

- [ ] **Step 1: Build + chạy app trên compose**

Run:
```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
docker compose up -d --build
sleep 8
docker compose logs fall_detection_web | tail -20
```
Expected: log có `Application startup complete` + `Creating default admin: admin/admin` (admin ghi vào bảng `users` Postgres), KHÔNG có traceback sqlite/psycopg.

- [ ] **Step 2: Verify auth + settings persist (users/settings trên Postgres)**

Run:
```bash
# login lấy cookie
curl -s -c /tmp/fdw_cookie -d "username=admin&password=admin" http://localhost:8090/login -o /dev/null -w "login=%{http_code}\n"
# health/dashboard sau login
curl -s -b /tmp/fdw_cookie -o /dev/null -w "dashboard=%{http_code}\n" http://localhost:8090/
# user trong Postgres
docker compose exec -T postgres psql -U dcnet -d dcnet -c "SELECT username FROM users;"
```
Expected: `login=303` (redirect sau login OK) hoặc `200`; `dashboard=200`; bảng `users` có dòng `admin`.

- [ ] **Step 3: Verify incident hiển thị (incidents trên Postgres)**

Run:
```bash
docker compose exec -T postgres psql -U dcnet -d dcnet -c "INSERT INTO incidents (time,time_local,status,camera,ai_result,message) VALUES (now()::text, now()::text, 'verified','cam1','EMERGENCY','manual test');"
curl -s -b /tmp/fdw_cookie "http://localhost:8090/api/events?limit=5" | head -c 400; echo
```
Expected: JSON trả về có dòng `camera":"cam1"` / `message":"manual test"` (app đọc bảng `incidents`).
> Nếu route khác `/api/events`, tra trong `app.py` route list — mục tiêu: 1 endpoint đọc incidents trả dữ liệu vừa insert.

- [ ] **Step 4: Cập nhật CLAUDE.md**

Trong `camera-ai/CLAUDE.md`, mục "Architecture — fall_detection_web" → dòng `db.py` (`[db.py](fall_detection_web/db.py) — SQLite (WAL mode)...`): đổi thành mô tả Postgres. Thay đoạn:

> `db.py` — SQLite (WAL mode) for `events`, `recordings` ...

bằng:

> `db.py` — **PostgreSQL (psycopg v3, ConnectionPool)** for `incidents` (bảng fall-detection cũ tên `events`, đổi để tránh va chạm counting), `users`, `settings`. DSN qua env `DATABASE_URL`/`DB_*`. Schema tạo trong `init_db` (tường minh, không migration framework). Bảng `recordings` = filter `incidents` theo cột video. (Phase 0 migration — xem `docs/superpowers/specs/2026-06-26-dcnet-platform-migration-design.md`.)

- [ ] **Step 5: Cập nhật AGENTS.md**

Trong `fall_detection_web/AGENTS.md`, tìm chỗ liệt kê stack/SQLite (vd "SQLite") → đổi mô tả storage sang **PostgreSQL (psycopg)**, ghi chú bảng `events`→`incidents`. (Giữ nguyên các ràng buộc khác.)

- [ ] **Step 6: Commit**

```bash
cd /Users/vovanduc/Code/dcnet/camera-ai
git add CLAUDE.md fall_detection_web/AGENTS.md
git commit -m "docs(phase0): cập nhật CLAUDE.md/AGENTS.md — DB Postgres, bảng incidents"
```

---

## Self-Review (đã chạy)

**Spec coverage:** P0.2 schema → Task 2 init_db ✅; P0.3 db.py psycopg → Task 2 ✅; P0.4 config DSN → Task 1 (.env) + db.py `_dsn()` ✅ (config.py không cần đổi — db.py đọc env trực tiếp, `get_all_settings` signature giữ); P0.5 migrate → SKIP (greenfield) ✅; P0.6 deploy → Task 1 compose ✅; P0.8 verification → Task 2 smoke + Task 3 integration ✅; P0.9 rủi ro (signature giữ, grep sqlite-ism, rename) → addressed ✅.

**Placeholder scan:** không có TBD/TODO; mọi step có code/lệnh cụ thể.

**Type consistency:** signature hàm public giữ y `db.py` cũ (đã đối chiếu Global Constraints). Row access đổi `row[0/1]`→`row["col"]` đồng bộ với `dict_row` toàn file. Bảng `incidents` dùng nhất quán mọi query. `_prune_events`/`find_matching_teldrive_image` nhận `psycopg.Connection` (đổi type hint, signature param giữ).

**Lưu ý người chạy:** `set_settings_bulk` dùng `cur.executemany` (psycopg3 hỗ trợ executemany trong cursor). `conn.execute()` shortcut của psycopg3 trả cursor — fetchone/fetchall hợp lệ.
