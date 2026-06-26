# services/reid_worker/src/reid_worker/main.py
"""reid_worker — object-snapshot MQTT → assemble → embed → match → pgvector.

Body Re-ID = trục chính; face = vote phụ (lưu để audit). Group còn sống trong TTL.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import shutil
import sys
from datetime import datetime, timezone

import aiomqtt
import asyncpg
import structlog

from reid_worker.assembler import Assembler
from reid_worker.embed import BodyEmbedder, FaceEmbedder, embed_appearance
from reid_worker.matcher import decide_match
from reid_worker.parser import parse_objsnap
from reid_worker.repo import ReidRepo


def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()),
                        stream=sys.stdout)
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars,
                    structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(fmt="iso", utc=True),
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
    )


log = structlog.get_logger("reid_worker")

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/app/data"))
CROP_DIR = DATA_DIR / "reid_crops"
TTL_HOURS = float(os.environ.get("REID_TTL_HOURS", "2"))
BODY_THRESHOLD = float(os.environ.get("REID_BODY_THRESHOLD", "0.6"))
TRACK_TIMEOUT_MS = int(os.environ.get("REID_TRACK_TIMEOUT_MS", "3000"))
FACE_QUALITY_MIN = float(os.environ.get("REID_FACE_QUALITY_MIN", "0.2"))
FACE_ENABLED = os.environ.get("REID_FACE_ENABLED", "false").lower() == "true"
# Body crop quality gate (data 2026-06-25: người-thật score 0.6–0.92, junk/cửa-kính ~0.0).
BODY_SCORE_MIN = float(os.environ.get("REID_BODY_SCORE_MIN", "0.5"))
BODY_MIN_W = int(os.environ.get("REID_BODY_MIN_W", "96"))
BODY_MIN_H = int(os.environ.get("REID_BODY_MIN_H", "192"))
CAM_UID = os.environ.get("CAM_UID", "B8A44F4627CE")


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return (f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _save_crops(group_id: int, appearance_id: int, crops: list) -> str | None:
    """Ghi crops xuống /data; trả path crop body đầu tiên (rep)."""
    d = CROP_DIR / str(group_id)
    d.mkdir(parents=True, exist_ok=True)
    rep = None
    for kind, jpeg, idx, _q in crops:
        p = d / f"{appearance_id}_{kind}_{idx}.jpg"
        p.write_bytes(jpeg)
        if rep is None and kind == "body":
            rep = str(p)
    return rep


async def process_appearance(ap: dict, repo: ReidRepo, cam_id: int,
                             body: BodyEmbedder, face: "FaceEmbedder | None") -> None:
    emb = embed_appearance(ap, body, face, body_score_min=BODY_SCORE_MIN,
                           body_min_w=BODY_MIN_W, body_min_h=BODY_MIN_H)
    if emb is None:
        log.info("appearance_no_embedding", track=ap["track_id"])
        return
    # Wall-clock: group last_seen/first_seen drive TTL window + purge (compared vs DB now()).
    # Camera ts_ms is unreliable (parse-fail → 0 → 1970 → group instantly purged, re-entry dies).
    ts = datetime.now(timezone.utc)

    groups = await repo.live_groups(cam_id, TTL_HOURS)
    decision = decide_match(emb["body_vector"], groups, BODY_THRESHOLD)

    if decision["group_id"] is None:
        gid = await repo.create_group(
            cam_id=cam_id, ts=ts, body_vec=emb["body_vector"],
            face_vec=emb["face_vector"], track_id=ap["track_id"], rep_crop_path=None)
        app_id = await repo.latest_appearance_id_for_group(gid)
        is_reentry = False
    else:
        gid = decision["group_id"]
        app_id = await repo.add_appearance_to_group(
            group_id=gid, cam_id=cam_id, ts=ts, body_vec=emb["body_vector"],
            face_vec=emb["face_vector"], track_id=ap["track_id"])
        is_reentry = True

    rep_path = _save_crops(gid, app_id, emb["crops"])
    if rep_path and decision["group_id"] is None:
        async with repo.pool.acquire() as c:
            await c.execute("UPDATE person_group SET rep_crop_path=$1 WHERE id=$2",
                            rep_path, gid)
    for kind, _jpeg, idx, q in emb["crops"]:
        p = CROP_DIR / str(gid) / f"{app_id}_{kind}_{idx}.jpg"
        await repo.insert_crop(appearance_id=app_id, kind=kind, path=str(p),
                               frame_idx=idx, quality=q)

    log.info("appearance_processed", group_id=gid, appearance_id=app_id,
             track=ap["track_id"], reentry=is_reentry,
             similarity=round(decision["similarity"], 3),
             n_body=len(ap["body_objs"]), n_face=len(ap["face_objs"]),
             face_vote=emb["face_vector"] is not None)


async def _supervise(name: str, coro_factory) -> None:
    """Run a loop forever; never let an unexpected exception escape (would kill siblings)."""
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("loop_crashed_restarting", loop=name)
            await asyncio.sleep(2)


async def purge_loop(repo: ReidRepo) -> None:
    while True:
        await asyncio.sleep(300)  # 5 phút
        try:
            gids = await repo.purge_expired(TTL_HOURS)
            for gid in gids:
                shutil.rmtree(CROP_DIR / str(gid), ignore_errors=True)
            if gids:
                log.info("purged_expired_groups", count=len(gids))
        except Exception:
            log.exception("purge_failed")


async def flush_loop(asm: Assembler, repo: ReidRepo, cam_id: int,
                     body: BodyEmbedder, face: "FaceEmbedder | None") -> None:
    while True:
        await asyncio.sleep(1)
        for ap in asm.flush_expired(_now_ms()):
            try:
                await process_appearance(ap, repo, cam_id, body, face)
            except Exception:
                log.exception("process_appearance_failed", track=ap.get("track_id"))


async def consume_loop(asm: Assembler) -> None:
    host = os.environ["MQTT_HOST"]
    port = int(os.environ.get("MQTT_PORT", "1883"))
    user = os.environ.get("MQTT_USER") or None
    pwd = os.environ.get("MQTT_PASSWORD") or None
    topic = os.environ.get("REID_TOPIC", "poc/objsnap")
    client_id = os.environ.get("MQTT_CLIENT_ID", "reid_worker_cameraai")
    tls = os.environ.get("MQTT_TLS", "false").lower() == "true"
    tls_params = aiomqtt.TLSParameters() if tls else None
    while True:
        try:
            async with aiomqtt.Client(hostname=host, port=port, username=user,
                                      password=pwd, identifier=client_id,
                                      tls_params=tls_params) as client:
                log.info("mqtt_connected", host=host, port=port, topic=topic, tls=tls)
                await client.subscribe(topic)
                async for msg in client.messages:
                    try:
                        obj = parse_objsnap(json.loads(msg.payload))
                        if obj is not None:
                            asm.add(obj, _now_ms())   # timeout theo arrival wall-clock
                    except Exception:
                        log.exception("msg_parse_failed")
        except aiomqtt.MqttError as exc:
            log.warning("mqtt_disconnected", error=str(exc))
            await asyncio.sleep(2)


async def amain() -> None:
    _configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    if os.environ.get("REID_COMMERCIAL_MODE", "false").lower() == "true":
        log.error("commercial_mode_blocked",
                  msg="OSNet/InsightFace = non-commercial/AGPL. Đổi stack permissive trước khi bán.")
        sys.exit(1)
    CROP_DIR.mkdir(parents=True, exist_ok=True)
    log.info("reid_worker_starting", ttl_hours=TTL_HOURS, threshold=BODY_THRESHOLD,
             face_enabled=FACE_ENABLED)
    body = BodyEmbedder()
    face = FaceEmbedder(quality_min=FACE_QUALITY_MIN) if FACE_ENABLED else None
    log.info("models_loaded")
    pool = await asyncpg.create_pool(_dsn(), min_size=2, max_size=4)
    repo = ReidRepo(pool)
    cam_id = await repo.cam_id_for(CAM_UID)
    if cam_id is None:
        log.error("cam_not_found", cam_uid=CAM_UID)
        sys.exit(1)
    asm = Assembler(track_timeout_ms=TRACK_TIMEOUT_MS)
    try:
        await asyncio.gather(
            _supervise("consume", lambda: consume_loop(asm)),
            _supervise("flush", lambda: flush_loop(asm, repo, cam_id, body, face)),
            _supervise("purge", lambda: purge_loop(repo)),
        )
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
