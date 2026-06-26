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
