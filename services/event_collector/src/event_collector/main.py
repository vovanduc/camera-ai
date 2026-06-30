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
from datetime import datetime, timezone
from typing import Any

import aiomqtt
import asyncpg
import httpx
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
    go2rtc_src  TEXT,
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
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS go2rtc_src TEXT;
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


async def _save_axis_snapshot(repo: Repo, cam_id: int, ev_id: int, direction: str) -> None:
    # Best-effort tuyệt đối: mọi lỗi (DB, fs, network) chỉ log, KHÔNG được
    # thoát ra làm gãy vòng MQTT consume.
    try:
        src = await repo.go2rtc_src_for(cam_id)
        if not src:
            return
        base = os.environ.get("GO2RTC_INTERNAL_URL", "http://go2rtc:1984")
        snaps = os.environ.get("COUNTING_SNAPS_DIR", "/app/data/counting_snaps")
        os.makedirs(snaps, exist_ok=True)
        url = f"{base.rstrip('/')}/api/frame.jpeg?src={src}"
        # go2rtc grab on-demand: nếu producer RTSP đang nguội (không consumer),
        # lần đầu có thể 500 do chưa kịp keyframe → retry 1 lần sau warm-up.
        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(url)
            if r.status_code != 200:
                await asyncio.sleep(0.4)
                r = await cli.get(url)
        if r.status_code == 200 and r.content:
            ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            fname = f"{ts}_axis_{direction}.jpg"
            fpath = os.path.join(snaps, fname)
            with open(fpath, "wb") as f:
                f.write(r.content)
            await repo.set_snapshot(ev_id, fpath)
    except Exception as exc:
        log.warning("axis_snapshot_failed", error=str(exc))


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
            await _save_axis_snapshot(repo, cam_id, ev_id, event["direction"])
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
