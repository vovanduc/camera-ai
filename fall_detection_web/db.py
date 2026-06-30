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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
EVENT_IMAGES_DIR = DATA_DIR / "event_images"
REID_CROPS_DIR = DATA_DIR / "reid_crops"
COUNTING_SNAPS_DIR = DATA_DIR / "counting_snaps"

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
    COUNTING_SNAPS_DIR.mkdir(parents=True, exist_ok=True)


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
        # ── Phase 2: Re-ID group schema (module optional, OFF mặc định) ──
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS person_group (
                id               BIGSERIAL PRIMARY KEY,
                cam_id           INT REFERENCES cameras(id),
                first_seen       TIMESTAMPTZ NOT NULL,
                last_seen        TIMESTAMPTZ NOT NULL,
                visit_count      INT NOT NULL DEFAULT 1,
                rep_body_vector  vector(512) NOT NULL,
                rep_face_vector  vector(512),
                rep_crop_path    TEXT,
                created_at       TIMESTAMPTZ DEFAULT now()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS person_group_last_seen ON person_group (last_seen DESC)")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS person_group_body_ivf ON person_group
            USING ivfflat (rep_body_vector vector_cosine_ops) WITH (lists = 100)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appearance (
                id           BIGSERIAL PRIMARY KEY,
                group_id     BIGINT REFERENCES person_group(id) ON DELETE CASCADE,
                cam_id       INT REFERENCES cameras(id),
                ts           TIMESTAMPTZ NOT NULL,
                body_vector  vector(512) NOT NULL,
                face_vector  vector(512),
                track_id     TEXT,
                created_at   TIMESTAMPTZ DEFAULT now()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS appearance_group ON appearance (group_id, ts DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appearance_crop (
                id             BIGSERIAL PRIMARY KEY,
                appearance_id  BIGINT REFERENCES appearance(id) ON DELETE CASCADE,
                kind           TEXT NOT NULL CHECK (kind IN ('body','face')),
                path           TEXT NOT NULL,
                frame_idx      INT,
                quality        REAL,
                created_at     TIMESTAMPTZ DEFAULT now()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS appearance_crop_app ON appearance_crop (appearance_id)")
        # ── Phase 3: module flags per-camera ──
        for col in ("counting_enabled", "fall_detection_enabled",
                    "reid_enabled", "live_enabled"):
            conn.execute(
                f"ALTER TABLE cameras ADD COLUMN IF NOT EXISTS {col} "
                "BOOLEAN NOT NULL DEFAULT false"
            )
        conn.execute("CREATE INDEX IF NOT EXISTS cameras_fall_det "
                     "ON cameras (fall_detection_enabled) WHERE enabled = true")
        conn.execute("CREATE INDEX IF NOT EXISTS cameras_counting "
                     "ON cameras (counting_enabled) WHERE enabled = true")
        # ── Unified registry: config columns formerly in settings-JSON cameras ──
        # (fall_detection pipeline / monitor.py reads these off each camera row)
        _CAMERA_CONFIG_COLS = (
            ("go2rtc_src", "TEXT NOT NULL DEFAULT ''"),
            ("live_url", "TEXT NOT NULL DEFAULT ''"),
            ("live_mode", "TEXT NOT NULL DEFAULT 'auto'"),
            ("prompt_id", "TEXT NOT NULL DEFAULT ''"),
            ("local_save_images", "BOOLEAN NOT NULL DEFAULT true"),
            ("local_save_videos", "BOOLEAN NOT NULL DEFAULT true"),
            ("teldrive_upload_images", "BOOLEAN NOT NULL DEFAULT true"),
            ("teldrive_record_enabled", "BOOLEAN NOT NULL DEFAULT false"),
            ("record_seconds", "INTEGER"),
            ("record_cooldown", "INTEGER"),
        )
        for col, ddl in _CAMERA_CONFIG_COLS:
            conn.execute(f"ALTER TABLE cameras ADD COLUMN IF NOT EXISTS {col} {ddl}")
        # Seed: cam Axis = đếm + live (idempotent, SET true an toàn re-run)
        conn.execute("UPDATE cameras SET counting_enabled=true, live_enabled=true "
                     "WHERE cam_uid='B8A44F4627CE'")
        # ── Dual-counting test: baseline reset + cấu hình vạch YOLO per-camera ──
        conn.execute(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS yolo_counting JSONB "
            "NOT NULL DEFAULT '{}'::jsonb"
        )
        # ── Verify-crop: crop khung verify vào người (conf cao nhất) + padding ──
        # Chỉ ảnh đưa AI bị crop; ảnh log/Telegram/snapshot live giữ full frame.
        conn.execute(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS verify_crop JSONB "
            "NOT NULL DEFAULT '{}'::jsonb"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS counting_baseline (
                cam_id    INT PRIMARY KEY REFERENCES cameras(id),
                reset_ts  TIMESTAMPTZ NOT NULL,
                baseline  INT NOT NULL CHECK (baseline >= 0)
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
    where = "e.type = 'counter' AND (e.ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date " \
            "= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date"
    params: tuple[Any, ...] = ()
    if cam_id is not None:
        where += " AND e.cam_id = %s"
        params = (cam_id,)
    sql = (
        "SELECT "
        "COUNT(*) FILTER (WHERE e.direction = 'in')  AS ins, "
        "COUNT(*) FILTER (WHERE e.direction = 'out') AS outs "
        "FROM events e JOIN cameras c ON c.id = e.cam_id "
        f"WHERE c.enabled = true AND c.counting_enabled = true AND {where}"
    )
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    ins = int(row["ins"] or 0)
    outs = int(row["outs"] or 0)
    return {"in": ins, "out": outs, "occupancy": max(0, ins - outs)}


def counting_crossings(day: date, cam_id: int | None = None) -> list[dict[str, Any]]:
    """Crossing rows của 1 ngày VN — cho bucket_hourly + log. ts trả về aware UTC."""
    where = "e.type = 'counter' AND (e.ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date = %s"
    params: list[Any] = [day]
    if cam_id is not None:
        where += " AND e.cam_id = %s"
        params.append(cam_id)
    sql = (
        f"SELECT e.ts, e.direction FROM events e JOIN cameras c ON c.id = e.cam_id "
        f"WHERE c.enabled = true AND c.counting_enabled = true AND {where} ORDER BY e.ts DESC"
    )
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [{"ts": r["ts"], "direction": r["direction"]} for r in rows]


_SOURCE_TYPE = {"yolo": "counter_yolo", "axis": "counter"}

_VN_TODAY = ("(e.ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date "
             "= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date")


def insert_counting_event(cam_id: int, direction: str, ts: datetime,
                          source: str = "yolo", track_id: str | None = None,
                          snapshot_path: str | None = None) -> None:
    """Ghi 1 crossing vào bảng events (dùng cho YOLO; source='axis' nếu cần)."""
    if random.random() < 0.02:
        cleanup_counting_snaps()
    etype = _SOURCE_TYPE.get(source, "counter_yolo")
    axis_obj = f"yolo-{track_id}" if track_id is not None else None
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO events (cam_id, ts, type, direction, axis_object_id, payload, snapshot_path) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (cam_id, ts, etype, direction, axis_obj,
             Json({"source": source, "track_id": track_id}),
             snapshot_path),
        )


def counting_log_today(cam_id: int, limit: int = 50) -> list[dict[str, Any]]:
    sql = ("SELECT type, direction, ts, snapshot_path FROM events "
           f"WHERE cam_id=%s AND type IN ('counter','counter_yolo') AND "
           "(ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date=(now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date "
           "ORDER BY ts DESC LIMIT %s")
    with get_conn() as conn:
        rows = conn.execute(sql, (cam_id, limit)).fetchall()
    out = []
    for r in rows:
        snap = Path(r["snapshot_path"]).name if r["snapshot_path"] else None
        out.append({"source": "yolo" if r["type"] == "counter_yolo" else "axis",
                    "direction": r["direction"],
                    "time": r["ts"].astimezone(LOCAL_TZ).strftime("%H:%M:%S"),
                    "snap": snap})
    return out


def cleanup_counting_snaps(max_age_seconds: int = 2 * 86400) -> None:
    ensure_data_dir()
    cutoff = time.time() - max_age_seconds
    for p in COUNTING_SNAPS_DIR.glob("*.jpg"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            continue


def counting_block(cam_id: int, source: str, since_ts: datetime | None = None,
                   baseline_in: int = 0) -> dict[str, int]:
    """IN/OUT/occupancy hôm nay VN cho 1 nguồn (counter | counter_yolo), 1 camera."""
    import counting as _counting
    etype = _SOURCE_TYPE.get(source, "counter_yolo")
    where = f"e.cam_id = %s AND e.type = %s AND {_VN_TODAY}"
    params: list[Any] = [cam_id, etype]
    if since_ts is not None:
        where += " AND e.ts > %s"
        params.append(since_ts)
    sql = (
        "SELECT COUNT(*) FILTER (WHERE e.direction = 'in')  AS ins, "
        "COUNT(*) FILTER (WHERE e.direction = 'out') AS outs "
        f"FROM events e WHERE {where}"
    )
    with get_conn() as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
    return _counting.block_from_counts(int(row["ins"] or 0), int(row["outs"] or 0), baseline_in)


def get_counting_baseline(cam_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT reset_ts, baseline FROM counting_baseline WHERE cam_id = %s",
            (cam_id,),
        ).fetchone()
    return {"reset_ts": row["reset_ts"], "baseline": int(row["baseline"])} if row else None


def set_counting_baseline(cam_id: int, reset_ts: datetime, baseline: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO counting_baseline (cam_id, reset_ts, baseline) VALUES (%s, %s, %s) "
            "ON CONFLICT (cam_id) DO UPDATE SET reset_ts = EXCLUDED.reset_ts, "
            "baseline = EXCLUDED.baseline",
            (cam_id, reset_ts, max(0, int(baseline))),
        )


def get_yolo_counting(cam_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT yolo_counting FROM cameras WHERE id = %s", (cam_id,)
        ).fetchone()
    return dict(row["yolo_counting"]) if row and row["yolo_counting"] else {}


def set_yolo_counting(cam_id: int, cfg: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE cameras SET yolo_counting = %s WHERE id = %s",
            (Json(cfg), cam_id),
        )


def set_verify_crop(cam_id: int, cfg: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE cameras SET verify_crop = %s WHERE id = %s",
            (Json(cfg), cam_id),
        )


def list_yolo_counting_cameras() -> list[dict[str, Any]]:
    """Cameras active có yolo_counting.enabled = true (cho engine đếm YOLO)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, rtsp_url, go2rtc_src, yolo_counting FROM cameras "
            "WHERE enabled = true AND COALESCE((yolo_counting->>'enabled')::bool, false) = true "
            "ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Re-ID groups (Phase 2, read-only; worker ghi) ──

def reid_live_groups(ttl_hours: float = 2, cam_id: int | None = None) -> list[dict[str, Any]]:
    where = "last_seen >= now() - (%s || ' hours')::interval"
    params: list[Any] = [str(ttl_hours)]
    if cam_id is not None:
        where += " AND cam_id = %s"
        params.append(cam_id)
    sql = (
        "SELECT id, visit_count, first_seen, last_seen, rep_crop_path "
        f"FROM person_group WHERE {where} ORDER BY last_seen DESC"
    )
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def reid_group_crops(group_id: int, limit: int = 40) -> list[dict[str, Any]]:
    sql = (
        "SELECT ac.kind, ac.path, ac.quality, a.ts "
        "FROM appearance a JOIN appearance_crop ac ON ac.appearance_id = a.id "
        "WHERE a.group_id = %s ORDER BY a.ts DESC, ac.kind LIMIT %s"
    )
    with get_conn() as conn:
        rows = conn.execute(sql, (group_id, limit)).fetchall()
    return [dict(r) for r in rows]


def reid_stats(ttl_hours: float = 2) -> dict[str, int]:
    sql = (
        "SELECT COUNT(*) AS unique_count, "
        "COUNT(*) FILTER (WHERE visit_count > 1) AS reentry_count "
        "FROM person_group WHERE last_seen >= now() - (%s || ' hours')::interval"
    )
    with get_conn() as conn:
        row = conn.execute(sql, (str(ttl_hours),)).fetchone()
    return {"unique_count": int(row["unique_count"] or 0),
            "reentry_count": int(row["reentry_count"] or 0)}


# ── Camera module flags (Phase 3) ──

_MODULE_COLS = {"counting", "fall_detection", "reid", "live"}


def list_cameras_for_module(module: str) -> list[dict[str, Any]]:
    """Cameras đang active có module bật. module ∈ counting|fall_detection|reid|live."""
    if module not in _MODULE_COLS:
        raise ValueError(f"unknown module: {module}")
    col = f"{module}_enabled"
    sql = (f"SELECT * FROM cameras WHERE enabled = true AND {col} = true ORDER BY id")
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def list_cameras_all() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, cam_uid, name, model, location, enabled, "
            "counting_enabled, fall_detection_enabled, reid_enabled, live_enabled "
            "FROM cameras ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def update_camera_modules(cam_id: int, modules: dict[str, bool]) -> None:
    """Cập nhật flag. modules keys ⊂ {counting,fall_detection,reid,live}."""
    sets, params = [], []
    for m in _MODULE_COLS:
        if m in modules:
            sets.append(f"{m}_enabled = %s")
            params.append(bool(modules[m]))
    if not sets:
        return
    params.append(cam_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE cameras SET {', '.join(sets)} WHERE id = %s", tuple(params))


# ── Unified camera registry CRUD (replaces settings-JSON cameras) ──

# Columns the unified /cameras UI may write. Whitelist = SQL-injection guard.
_CAMERA_EDITABLE = (
    "name", "rtsp_url", "go2rtc_src", "mjpeg_url", "live_url", "live_mode",
    "prompt_id", "vendor", "model", "location", "enabled",
    "local_save_images", "local_save_videos", "teldrive_upload_images",
    "teldrive_record_enabled", "record_seconds", "record_cooldown",
    "counting_enabled", "fall_detection_enabled", "reid_enabled", "live_enabled",
)


def _slugify(text: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    return s[:24] or "cam"


def gen_cam_uid(name: str) -> str:
    """Synthetic cam_uid for manually-added (non-edge) cameras."""
    import uuid
    return f"manual-{_slugify(name)}-{uuid.uuid4().hex[:6]}"


def cameras_for_config() -> list[dict[str, Any]]:
    """All cameras as dicts (id, cam_uid, config cols, module flags).

    config.read_config() finalizes these into monitor's camera dict shape.
    NOT filtered by any flag — the unified UI and snapshot/live routes need
    every camera; the monitor selector filters fall_detection_enabled itself.
    """
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM cameras ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def insert_camera(fields: dict[str, Any]) -> int:
    cols = {k: v for k, v in fields.items() if k in _CAMERA_EDITABLE}
    cols.setdefault("rtsp_url", "")          # NOT NULL — go2rtc-only cams have no rtsp
    name = str(cols.get("name") or "Camera").strip() or "Camera"
    cols["name"] = name
    cam_uid = str(fields.get("cam_uid") or "").strip() or gen_cam_uid(name)
    colnames = ["cam_uid"] + list(cols.keys())
    placeholders = ", ".join(["%s"] * len(colnames))
    params = [cam_uid] + list(cols.values())
    with get_conn() as conn:
        row = conn.execute(
            f"INSERT INTO cameras ({', '.join(colnames)}) VALUES ({placeholders}) "
            "RETURNING id", tuple(params)
        ).fetchone()
    return int(row["id"])


def update_camera(cam_id: int, fields: dict[str, Any]) -> None:
    sets, params = [], []
    for k, v in fields.items():
        if k in _CAMERA_EDITABLE:
            sets.append(f"{k} = %s")
            params.append(v)
    if not sets:
        return
    params.append(cam_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE cameras SET {', '.join(sets)} WHERE id = %s", tuple(params))


def camera_has_history(cam_id: int) -> bool:
    """True if FK rows (events/appearance) reference this camera — block hard delete."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT (EXISTS(SELECT 1 FROM events WHERE cam_id=%s) "
            "OR EXISTS(SELECT 1 FROM appearance WHERE cam_id=%s)) AS h",
            (cam_id, cam_id),
        ).fetchone()
    return bool(row["h"])


def delete_camera(cam_id: int) -> str:
    """Hard-delete if no FK history, else soft-disable. Returns 'deleted'|'disabled'."""
    if camera_has_history(cam_id):
        with get_conn() as conn:
            conn.execute("UPDATE cameras SET enabled = false WHERE id = %s", (cam_id,))
        return "disabled"
    with get_conn() as conn:
        conn.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))
    return "deleted"
