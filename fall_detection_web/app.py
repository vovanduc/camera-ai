"""Main FastAPI application — Fall Detection Web."""

from __future__ import annotations

import logging
import os
import secrets
import psutil
from contextlib import asynccontextmanager
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request, Response, status, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import ai
import auth
import config
import counting
import db
import monitor
import teldrive
import redis_cache

logger = logging.getLogger("fall_detection_web")
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

# Setup Jinja2 templates
templates = Jinja2Templates(directory=str(ROOT / "templates"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    db.init_db()
    db.delete_old_events(7)        # clean up old events/images at startup
    config.migrate_config_json()   # one-time: config.json → DB
    config.migrate_cameras_to_table()  # one-time: settings-JSON cameras → cameras table
    current_config = config.read_config()
    monitor.start_local_clips_maintenance(current_config)
    
    # Init auth secret from config or env
    jwt_secret = current_config.get("jwt_secret")
    if not jwt_secret:
        # Fallback: check persistent file
        secret_file = DATA_DIR / ".secret_key"
        if secret_file.exists():
            jwt_secret = secret_file.read_text(encoding="utf-8").strip()
        else:
            jwt_secret = secrets.token_urlsafe(32)
            secret_file.write_text(jwt_secret, encoding="utf-8")
    
    auth.configure_secret(jwt_secret)
    
    # Create default admin if no users
    if not db.list_users():
        logger.info("No users found. Creating default admin: admin/admin")
        db.create_user("admin", auth.hash_password("admin"))
    
    # Auto-start monitor
    try:
        if monitor.start_monitor(current_config) == "started":
            logger.info("Auto-started YOLO monitor on boot.")
    except ValueError as exc:
        logger.warning("Skipping auto-start monitor: %s", exc)
    except Exception as exc:
        logger.error("Could not auto-start monitor: %s", exc)

    # Auto-start engine đếm YOLO (độc lập với monitor fall-detect)
    try:
        monitor.start_counting(current_config)
    except Exception as exc:
        logger.error("Could not auto-start counting engine: %s", exc)

    yield
    # Shutdown
    monitor.stop_local_clips_maintenance(wait=True)
    monitor.stop_monitor(wait=True)
    monitor.stop_counting(wait=True)


app = FastAPI(title="Fall Detection Web", lifespan=lifespan)

DATA_DIR.mkdir(parents=True, exist_ok=True)
db.EVENT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "teldrive_cache").mkdir(parents=True, exist_ok=True)

@app.get("/favicon.ico")
async def favicon():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <rect width="100" height="100" rx="20" fill="#020617"/>
        <path d="M50 16l28 12v21c0 19-11.2 33.6-28 40-16.8-6.4-28-21-28-40V28l28-12z" fill="#0f172a" stroke="#22c55e" stroke-width="5"/>
        <path d="M30 52h10l5-14 9 28 5-14h11" fill="none" stroke="#22c55e" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>'''
    return Response(content=svg, media_type="image/svg+xml")

# ──────────────────────────────────────────────
# UI Routes
# ──────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={})

@app.post("/auth/login")
def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
):
    user = db.get_user(username)
    if not user or not auth.verify_password(password, str(user["password_hash"])):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid username or password.", "username": username},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    
    expire_hours = 24 * 30 if remember else 8
    token = auth.create_token(username, expire_hours=expire_hours)
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        max_age=expire_hours * 3600,
        samesite="lax",
    )
    return response

@app.post("/auth/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("session")
    return response

@app.get("/", response_class=HTMLResponse)
def index_page(request: Request, _: str = Depends(auth.require_auth)):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/cameras", response_class=HTMLResponse)
def cameras_page(request: Request, _: str = Depends(auth.require_auth)):
    return templates.TemplateResponse(request=request, name="cameras.html", context={"active_nav": "cameras"})


@app.get("/camera/{camera_name:path}", response_class=HTMLResponse)
def camera_page(request: Request, camera_name: str, _: str = Depends(auth.require_auth)):
    if not camera_name.strip():
        return RedirectResponse(url="/cameras", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request=request, name="camera_detail.html", context={"camera_name": camera_name, "active_nav": "cameras"})


@app.get("/counting", response_class=HTMLResponse)
def counting_page(request: Request, _: str = Depends(auth.require_auth)):
    return templates.TemplateResponse(request=request, name="counting.html", context={"active_nav": "counting"})


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


def _cam_id_by_name(camera_name: str) -> tuple[int, dict[str, Any]]:
    c = config.read_config()
    _, camera = find_camera_by_name(c, camera_name)
    cam_id = camera.get("id")
    if cam_id is None:
        raise HTTPException(status_code=404, detail="Camera chưa có trong registry")
    return int(cam_id), camera


def _counting_blocks(cam_id: int) -> dict[str, Any]:
    from datetime import datetime, timezone, timedelta
    vn = timezone(timedelta(hours=7))
    base = db.get_counting_baseline(cam_id)
    vn_today = datetime.now(vn).date()
    since_ts = None
    baseline_in = 0
    reset_ts_iso = None
    if base and base["reset_ts"].astimezone(vn).date() == vn_today:
        since_ts = base["reset_ts"]
        baseline_in = base["baseline"]
        reset_ts_iso = base["reset_ts"].astimezone(vn).strftime("%H:%M:%S")
    return {
        "date": vn_today.isoformat(),
        "camera": db.counting_block(cam_id, "axis", since_ts, baseline_in),
        "yolo": db.counting_block(cam_id, "yolo", since_ts, baseline_in),
        "reset_ts": reset_ts_iso,
        "log": [{**x, "snap_url": (f"/api/counting-snap/{x['snap']}" if x["snap"] else None)}
                for x in db.counting_log_today(cam_id)],
    }


@app.get("/api/counting/camera/{camera_name:path}")
def api_counting_camera(camera_name: str, _: str = Depends(auth.require_auth)):
    cam_id, _camera = _cam_id_by_name(camera_name)
    return _counting_blocks(cam_id)


@app.post("/api/counting/reset/{camera_name:path}")
def api_counting_reset(camera_name: str, payload: dict[str, Any] = Body(...),
                       _: str = Depends(auth.require_auth)):
    cam_id, _camera = _cam_id_by_name(camera_name)
    try:
        occupancy = max(0, int(payload.get("occupancy", 0)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="occupancy phải là số nguyên >= 0")
    from datetime import datetime, timezone
    db.set_counting_baseline(cam_id, datetime.now(timezone.utc), occupancy)
    return _counting_blocks(cam_id)


@app.get("/api/counting-snap/{filename}")
def counting_snap(filename: str, _: str = Depends(auth.require_auth)):
    safe = Path(filename).name
    if safe != filename or not safe.lower().endswith(".jpg"):
        raise HTTPException(status_code=404, detail="Not found")
    path = db.COUNTING_SNAPS_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400, immutable"})


# Allowlist model YOLO cho per-cam override (YOLO(name) nạp file weights).
_YOLO_MODEL_ALLOWLIST = {
    "yolov8n.pt", "yolov8s.pt", "yolov8m.pt", "yolov8l.pt", "yolov8x.pt",
    "yolo11n.pt", "yolo11s.pt", "yolo11m.pt", "yolo11l.pt", "yolo11x.pt",
}


def _build_yolo_cfg(payload: dict[str, Any]) -> dict[str, Any]:
    """Dựng + validate yolo_counting từ payload/query. Raise HTTPException nếu sai.
    Dùng chung cho POST (lưu DB) và preview (vẽ thử)."""
    def _pct(key: str, default: float) -> float:
        try:
            return min(100.0, max(0.0, float(payload.get(key, default))))
        except (TypeError, ValueError):
            return default

    x_start = _pct("x_start", 0)
    x_end = _pct("x_end", 100)
    if x_start >= x_end:
        raise HTTPException(status_code=400, detail="x_start phải nhỏ hơn x_end")
    cfg: dict[str, Any] = {
        "enabled": bool(payload.get("enabled", False)),
        "line_y": _pct("line_y", 50),
        "x_start": x_start,
        "x_end": x_end,
        "min_disp": _pct("min_disp", 6),
        "invert": bool(payload.get("invert", False)),
    }

    # ROI zoom-zone (tuỳ chọn) — crop vùng choke xa trước detect. Khi BẬT, line_y/x_start/x_end
    # diễn giải theo % TRONG ROI. roi_y1 < line_y < roi_y2 nếu không sẽ 0 crossing (xem doc §3).
    if bool(payload.get("roi_enabled", False)):
        roi_x1, roi_x2 = _pct("roi_x1", 0), _pct("roi_x2", 100)
        roi_y1, roi_y2 = _pct("roi_y1", 0), _pct("roi_y2", 100)
        if roi_x1 >= roi_x2 or roi_y1 >= roi_y2:
            raise HTTPException(status_code=400, detail="ROI không hợp lệ: x1<x2 và y1<y2")
        cfg.update({"roi_enabled": True, "roi_x1": roi_x1, "roi_y1": roi_y1,
                    "roi_x2": roi_x2, "roi_y2": roi_y2})

    # imgsz per-cam (đòn bẩy recall cho choke xa) — override global khi >0.
    raw_imgsz = payload.get("imgsz")
    if raw_imgsz not in (None, "", 0, "0"):
        try:
            cfg["imgsz"] = max(64, min(1920, int(raw_imgsz)))
        except (TypeError, ValueError):
            pass

    # model per-cam — allowlist cứng (YOLO(name) nạp file → chặn path/URL tuỳ ý).
    raw_model = str(payload.get("model") or "").strip()
    if raw_model:
        if raw_model not in _YOLO_MODEL_ALLOWLIST:
            raise HTTPException(status_code=400, detail=f"model không hợp lệ: {raw_model}")
        cfg["model"] = raw_model

    # conf per-cam — override global khi >0, clamp (0,1].
    raw_conf = payload.get("conf")
    if raw_conf not in (None, "", 0, "0"):
        try:
            cfg["conf"] = max(0.01, min(1.0, float(raw_conf)))
        except (TypeError, ValueError):
            pass
    return cfg


@app.post("/api/counting/yolo-config/{camera_name:path}")
def api_counting_yolo_config(camera_name: str, payload: dict[str, Any] = Body(...),
                             _: str = Depends(auth.require_auth)):
    cam_id, _camera = _cam_id_by_name(camera_name)
    cfg = _build_yolo_cfg(payload)
    db.set_yolo_counting(cam_id, cfg)
    monitor.restart_counting(config.read_config())
    return {"ok": True, "yolo_counting": cfg}


@app.post("/api/camera/verify-crop/{camera_name:path}")
def api_verify_crop_config(camera_name: str, payload: dict[str, Any] = Body(...),
                           _: str = Depends(auth.require_auth)):
    """Bật/tắt crop ảnh đưa AI vào người (conf cao nhất) + padding, per-camera.

    padding = fraction của w/h bbox (0–1). Chỉ ảnh AI bị crop; ảnh
    log/Telegram/snapshot live giữ full frame (xem monitor._monitor_loop).
    """
    cam_id, _camera = _cam_id_by_name(camera_name)
    try:
        padding = float(payload.get("padding", 0.15))
    except (TypeError, ValueError):
        padding = 0.15
    # Vùng loại trừ: list [x1,y1,x2,y2] (%) — bỏ detect trong TV/màn hình.
    zones = []
    for z in (payload.get("ignore_zones") or []):
        try:
            x1, y1, x2, y2 = (min(100.0, max(0.0, float(v))) for v in z)
        except (TypeError, ValueError):
            continue
        if x2 > x1 and y2 > y1:
            zones.append([x1, y1, x2, y2])
    cfg = {
        "enabled": bool(payload.get("enabled", False)),
        "padding": min(1.0, max(0.0, padding)),
        "ignore_zones": zones,
    }
    db.set_verify_crop(cam_id, cfg)
    _refresh_monitor_after_camera_change()
    return {"ok": True, "verify_crop": cfg}


@app.get("/api/counting/preview/{camera_name:path}")
def api_counting_preview(camera_name: str, request: Request,
                         _: str = Depends(auth.require_auth)):
    """Vẽ ROI+vạch+box detect lên frame hiện tại → JPEG. Calibrate vạch trực quan."""
    _cam_id, camera = _cam_id_by_name(camera_name)
    payload = dict(request.query_params)
    # query string: ép bool cho roi_enabled/invert/enabled (đến dạng "true"/"false").
    for k in ("roi_enabled", "invert", "enabled"):
        if k in payload:
            payload[k] = str(payload[k]).lower() in ("1", "true", "yes", "on")
    cfg = _build_yolo_cfg(payload)
    try:
        jpeg = monitor.counting_preview(camera, cfg, config.read_config())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không tạo được preview: {exc}")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


def _reid_enabled() -> bool:
    return os.environ.get("REID_ENABLED", "false").lower() == "true"


@app.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request, _: str = Depends(auth.require_auth)):
    return templates.TemplateResponse(
        request=request, name="groups.html",
        context={"reid_enabled": _reid_enabled(), "active_nav": "groups"})


@app.get("/api/groups")
def api_groups(_: str = Depends(auth.require_auth)):
    from datetime import timezone, timedelta
    vn = timezone(timedelta(hours=7))
    groups = db.reid_live_groups()
    stats = db.reid_stats()
    out = []
    for g in groups:
        gid = g["id"]
        crops = db.reid_group_crops(gid)
        rep = g.get("rep_crop_path")
        rep_name = Path(rep).name if rep else None
        out.append({
            "id": gid,
            "visit_count": g["visit_count"],
            "is_reentry": g["visit_count"] > 1,
            "badge": "🔁 ĐÃ VÀO RỒI" if g["visit_count"] > 1 else "🆕 Khách mới",
            "first_seen": g["first_seen"].astimezone(vn).strftime("%H:%M:%S %d/%m"),
            "last_seen": g["last_seen"].astimezone(vn).strftime("%H:%M:%S %d/%m"),
            "rep_crop": rep_name,
            "crops": [
                {"kind": c["kind"],
                 "name": Path(c["path"]).name,
                 "quality": round(float(c["quality"]), 2) if c["quality"] is not None else None,
                 "ts": c["ts"].astimezone(vn).strftime("%H:%M:%S")}
                for c in crops
            ],
        })
    return {"reid_enabled": _reid_enabled(), "stats": stats, "groups": out}


@app.get("/api/reid-crop/{group_id}/{filename}")
def reid_crop(group_id: str, filename: str, _: str = Depends(auth.require_auth)):
    if not group_id.isdigit():
        raise HTTPException(status_code=404, detail="Not found")
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith(".jpg"):
        raise HTTPException(status_code=404, detail="Not found")
    path = db.REID_CROPS_DIR / group_id / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400, immutable"})


@app.get("/modules")
def modules_page(_: str = Depends(auth.require_auth)):
    # Module management folded into the unified /cameras page.
    return RedirectResponse(url="/cameras", status_code=status.HTTP_302_FOUND)


@app.get("/api/camera-modules")
def api_camera_modules(_: str = Depends(auth.require_auth)):
    return {"cameras": db.list_cameras_all()}


@app.post("/api/camera-modules/{cam_id}")
def api_update_camera_modules(cam_id: int, payload: dict[str, Any] = Body(...),
                              _: str = Depends(auth.require_auth)):
    modules = {m: bool(payload.get(m, False))
               for m in ("counting", "fall_detection", "reid", "live")
               if m in payload}
    db.update_camera_modules(cam_id, modules)
    # CRITICAL: monitor binds its camera list at start; toggling fall_detection
    # is a no-op until the monitor restarts. Refresh it so the toggle takes effect.
    _refresh_monitor_after_camera_change()
    return {"ok": True, "cam_id": cam_id, "modules": modules}


@app.get("/api/auth/check")
def auth_check(_: str = Depends(auth.require_auth)):
    # Caddy forward_auth target: 200 nếu session JWT hợp lệ, 401 nếu không.
    # Dùng để gate /live/* (go2rtc) + /cam/* sau khi bỏ Caddy basic_auth (Phase 4 flip).
    return {"ok": True}


@app.get("/{page_name}", response_class=HTMLResponse)
def app_page(request: Request, page_name: str, _: str = Depends(auth.require_auth)):
    if page_name not in {"dashboard", "prompts", "live", "settings", "tools"}:
        raise HTTPException(status_code=404, detail="Page not found")
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/api/event-image/{filename}")
def event_image(request: Request, filename: str, _: str = Depends(auth.require_auth)):
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith(".jpg"):
        raise HTTPException(status_code=404, detail="Image not found")
    path = db.EVENT_IMAGES_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    stat = path.stat()
    etag = f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    last_modified = formatdate(stat.st_mtime, usegmt=True)
    headers = {
        "Cache-Control": "private, max-age=86400, immutable",
        "ETag": etag,
        "Last-Modified": last_modified,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    if_modified_since = request.headers.get("if-modified-since")
    if if_modified_since:
        try:
            since = parsedate_to_datetime(if_modified_since)
            if since.timestamp() >= int(stat.st_mtime):
                return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        except (TypeError, ValueError, OSError):
            pass
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers=headers,
    )


@app.get("/api/teldrive/file/{file_id}/{file_name:path}")
def teldrive_file(request: Request, file_id: str, file_name: str, _: str = Depends(auth.require_auth)):
    try:
        c = config.read_config()
        range_header = request.headers.get("range", "")
        guessed_media_type = teldrive._mime_type(file_name)
        is_image = guessed_media_type.startswith("image/") or Path(file_name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}

        if is_image:
            cache_dir = DATA_DIR / "teldrive_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / file_id
            
            if cache_path.exists():
                stat = cache_path.stat()
                etag = f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
                last_modified = formatdate(stat.st_mtime, usegmt=True)
                headers = {
                    "Cache-Control": "private, max-age=86400, immutable",
                    "ETag": etag,
                    "Last-Modified": last_modified,
                }
                if request.headers.get("if-none-match") == etag:
                    return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
                return FileResponse(
                    cache_path,
                    media_type=guessed_media_type,
                    headers=headers,
                )
            
            try:
                response = teldrive.download_file(c, file_id, file_name, range_header)
                content = response.content
                response.close()
                cache_path.write_bytes(content)
                stat = cache_path.stat()
                etag = f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
                last_modified = formatdate(stat.st_mtime, usegmt=True)
                headers = {
                    "Cache-Control": "private, max-age=86400, immutable",
                    "ETag": etag,
                    "Last-Modified": last_modified,
                }
                return FileResponse(
                    cache_path,
                    media_type=guessed_media_type,
                    headers=headers,
                )
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code in (401, 403):
                    return RedirectResponse(url=teldrive.file_url(c, file_id, file_name), status_code=status.HTTP_302_FOUND)
                raise

        # Non-image files (videos) stream as usual without caching
        response = teldrive.download_file(c, file_id, file_name, range_header)
        upstream_media_type = response.headers.get("content-type", "application/octet-stream")
        media_type = upstream_media_type
        if media_type == "application/octet-stream" or (
            guessed_media_type.startswith("video/") and not media_type.startswith("video/")
        ):
            media_type = guessed_media_type
        headers = {"Cache-Control": "private, max-age=300"}
        passthrough_headers = {
            "accept-ranges": "Accept-Ranges",
            "content-length": "Content-Length",
            "content-range": "Content-Range",
            "etag": "ETag",
            "last-modified": "Last-Modified",
        }
        for name, header_name in passthrough_headers.items():
            value = response.headers.get(name)
            if value:
                headers[header_name] = value
        if media_type.startswith("video/"):
            headers["Content-Disposition"] = f'inline; filename="{file_name}"'
            headers["Accept-Ranges"] = response.headers.get("accept-ranges", "bytes")
        else:
            content_disposition = response.headers.get("content-disposition")
            if content_disposition:
                headers["Content-Disposition"] = content_disposition
        def stream_body():
            try:
                yield from response.iter_content(chunk_size=1024 * 256)
            finally:
                response.close()
        return StreamingResponse(
            stream_body(),
            status_code=response.status_code,
            media_type=media_type,
            headers=headers,
        )
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (401, 403):
            c = config.read_config()
            return RedirectResponse(url=teldrive.file_url(c, file_id, file_name), status_code=status.HTTP_302_FOUND)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ──────────────────────────────────────────────
# API Routes
# ──────────────────────────────────────────────

@app.post("/api/user/update")
def update_user_credentials(
    response: Response,
    payload: dict[str, str] = Body(...),
    username: str = Depends(auth.require_auth)
):
    new_user = payload.get("username", "").strip()
    new_pass = payload.get("password", "")
    if not new_user or not new_pass:
        raise HTTPException(status_code=400, detail="Username and password are required")
    
    try:
        db.update_user(username, new_user, auth.hash_password(new_pass))
        # Issuing new token since username changed
        new_token = auth.create_token(new_user)
        response.set_cookie(
            key="session",
            value=new_token,
            httponly=True,
            samesite="lax",
            max_age=auth._SESSION_HOURS * 3600
        )
        return {"success": True, "message": "Credentials updated"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Failed to update credentials. Username might already exist.")

@app.get("/api/config")
def get_config(_: str = Depends(auth.require_auth)):
    return {"success": True, "config": config.read_config()}

@app.post("/api/config")
def save_config(new_config: dict[str, Any] = Body(...), _: str = Depends(auth.require_auth)):
    try:
        updated = config.write_config(new_config)
        monitor.schedule_uploaded_local_clips_cleanup(updated, reason="settings_saved")
        state = monitor.read_state()
        if state.get("running"):
            monitor.restart_monitor(updated)
            message = "Settings saved and monitor restarted"
        elif monitor.has_enabled_rtsp_camera(updated):
            result = monitor.start_monitor(updated)
            message = "Settings saved and monitor started" if result == "started" else f"Settings saved and monitor {result}"
        else:
            message = "Settings saved"
        return {"success": True, "config": updated, "message": message}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/cameras")
def get_cameras(_: str = Depends(auth.require_auth)):
    c = config.read_config()
    return {
        "success": True,
        "cameras": c.get("cameras", []),
        "prompts": c.get("prompts", []),
        "go2rtc_url": c.get("go2rtc_url", ""),
    }


def find_camera_by_name(c: dict[str, Any], camera_name: str) -> tuple[int, dict[str, Any]]:
    needle = str(camera_name or "").strip()
    cameras = c.get("cameras", [])
    for index, camera in enumerate(cameras):
        aliases = [
            camera.get("name"),
            camera.get("go2rtc_src"),
            camera.get("rtsp_url"),
            camera.get("live_url"),
        ]
        if any(str(alias or "").strip() == needle for alias in aliases):
            return index, camera
    raise HTTPException(status_code=404, detail="Camera not found")


@app.get("/api/camera/detail/{camera_name:path}")
def get_camera_detail(camera_name: str, _: str = Depends(auth.require_auth)):
    c = config.read_config()
    index, camera = find_camera_by_name(c, camera_name)
    return {
        "success": True,
        "index": index,
        "camera": camera,
        "prompts": c.get("prompts", []),
        "go2rtc_url": c.get("go2rtc_url", ""),
        "status": monitor.read_state(),
    }


# Keys the unified /cameras UI may submit per camera (config + module flags).
_CAMERA_SUBMIT_KEYS = (
    "name", "rtsp_url", "go2rtc_src", "mjpeg_url", "live_url", "live_mode",
    "prompt_id", "vendor", "model", "location", "enabled",
    "local_save_images", "local_save_videos", "teldrive_upload_images",
    "teldrive_record_enabled", "record_seconds", "record_cooldown",
    "counting_enabled", "fall_detection_enabled", "reid_enabled", "live_enabled",
)


def _refresh_monitor_after_camera_change() -> str:
    """Re-read config from the table and restart/start the monitor so camera
    edits and module toggles take effect immediately. Returns a status word."""
    updated = config.read_config()
    monitor.schedule_uploaded_local_clips_cleanup(updated, reason="cameras_saved")
    try:
        monitor.restart_counting(updated)
    except Exception as exc:
        logger.warning("counting engine restart failed: %s", exc)
    if monitor.read_state().get("running"):
        monitor.restart_monitor(updated)
        return "restarted"
    if monitor.has_enabled_rtsp_camera(updated):
        return monitor.start_monitor(updated)
    return "idle"


@app.post("/api/cameras")
def save_cameras(payload: dict[str, Any] = Body(...), _: str = Depends(auth.require_auth)):
    try:
        incoming = payload.get("cameras", [])
        if not isinstance(incoming, list):
            raise ValueError("cameras must be a list")
        existing_ids = {int(c["id"]) for c in db.cameras_for_config()}
        seen: set[int] = set()
        for cam in incoming:
            if not isinstance(cam, dict):
                continue
            fields = {k: cam[k] for k in _CAMERA_SUBMIT_KEYS if k in cam}
            cid = cam.get("id")
            if cid is not None and int(cid) in existing_ids:
                db.update_camera(int(cid), fields)
                seen.add(int(cid))
            else:
                seen.add(db.insert_camera(fields))
        # Cameras removed from the list → delete (soft if they have history).
        for cid in existing_ids - seen:
            db.delete_camera(cid)

        result = _refresh_monitor_after_camera_change()
        message = {"restarted": "Cameras saved and monitor restarted",
                   "started": "Cameras saved and monitor started",
                   "idle": "Cameras saved"}.get(result, f"Cameras saved (monitor {result})")
        updated = config.read_config()
        return {
            "success": True,
            "message": message,
            "cameras": updated.get("cameras", []),
            "prompts": updated.get("prompts", []),
            "go2rtc_url": updated.get("go2rtc_url", ""),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/teldrive/check")
def check_teldrive_token(payload: dict[str, Any] = Body(default={}), _: str = Depends(auth.require_auth)):
    try:
        c = config.read_config()
        token = str(payload.get("token", "")).strip() or None
        base_url = str(payload.get("base_url", "")).strip() or None
        result = teldrive.check_token(c, token=token, base_url=base_url)
        return {"success": True, "result": result}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/status")
def get_status(_: str = Depends(auth.require_auth)):
    c = config.read_config()
    if c.get("redis_enabled"):
        cached = redis_cache.get_cache("status:data", c)
        if cached:
            import json
            try:
                return json.loads(cached)
            except Exception:
                pass

    disk = psutil.disk_usage('/')
    data = {
        "success": True,
        "status": monitor.read_state(),
        "event_count": db.count_events(),
        "system": {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_percent": psutil.virtual_memory().percent,
            "disk_percent": disk.percent,
            "disk_used_gb": round(disk.used / (1024**3), 1),
            "disk_total_gb": round(disk.total / (1024**3), 1),
        }
    }

    if c.get("redis_enabled"):
        import json
        try:
            redis_cache.set_cache("status:data", json.dumps(data), 2, c)
        except Exception:
            pass

    return data

@app.post("/api/start")
def api_start_monitor(_: str = Depends(auth.require_auth)):
    try:
        result = monitor.start_monitor(config.read_config())
        return {"success": True, "message": f"Monitor {result}"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/stop")
def api_stop_monitor(_: str = Depends(auth.require_auth)):
    monitor.stop_monitor()
    return {"success": True, "message": "Monitor stopped"}

@app.get("/api/events")
def get_events(
    page: int = 1,
    limit: int = 10,
    ai_result: str | None = None,
    camera: str | None = None,
    status: str | None = None,
    _: str = Depends(auth.require_auth)
):
    if page < 1:
        page = 1
    if limit < 1 or limit > 500:
        limit = 10
        
    # Clean up empty strings from query params
    if ai_result == "" or ai_result == "All":
        ai_result = None
    if camera == "" or camera == "All":
        camera = None
    if status == "" or status == "All":
        status = None

    c = config.read_config()
    cache_key = f"events:list:{page}:{limit}:{ai_result or 'all'}:{camera or 'all'}:{status or 'all'}"
    if c.get("redis_enabled"):
        cached = redis_cache.get_cache(cache_key, c)
        if cached:
            import json
            try:
                return json.loads(cached)
            except Exception:
                pass
        
    offset = (page - 1) * limit
    events = db.get_events(limit=limit, offset=offset, ai_result=ai_result, camera=camera, status=status)
    total = db.get_events_total(ai_result=ai_result, camera=camera, status=status)
    
    data = {
        "success": True, 
        "events": events,
        "total": total,
        "page": page,
        "limit": limit
    }

    if c.get("redis_enabled"):
        import json
        try:
            redis_cache.set_cache(cache_key, json.dumps(data), 3600, c)
        except Exception:
            pass

    return data


@app.get("/api/events/trends")
def get_events_trends(_: str = Depends(auth.require_auth)):
    c = config.read_config()
    cache_key = "events:trends"
    if c.get("redis_enabled"):
        cached = redis_cache.get_cache(cache_key, c)
        if cached:
            import json
            try:
                return json.loads(cached)
            except Exception:
                pass

    trends = db.get_incident_trends(days=7)
    data = {
        "success": True,
        "trends": trends
    }

    if c.get("redis_enabled"):
        import json
        try:
            redis_cache.set_cache(cache_key, json.dumps(data), 3600, c)
        except Exception:
            pass

    return data



@app.get("/api/recordings")
def get_recordings(
    page: int = 1,
    limit: int = 10,
    camera: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    _: str = Depends(auth.require_auth)
):
    if page < 1:
        page = 1
    if limit < 1 or limit > 500:
        limit = 10
    if camera == "" or camera == "All":
        camera = None

    c = config.read_config()
    cache_key = f"recordings:list:{page}:{limit}:{camera or 'all'}:{date_from or 'all'}:{date_to or 'all'}"
    if c.get("redis_enabled"):
        cached = redis_cache.get_cache(cache_key, c)
        if cached:
            import json
            try:
                return json.loads(cached)
            except Exception:
                pass

    offset = (page - 1) * limit
    recordings = db.get_recordings(limit=limit, offset=offset, camera=camera, date_from=date_from, date_to=date_to)
    for item in recordings:
        video_id = str(item.get("teldrive_video_id", "")).strip()
        video_name = str(item.get("teldrive_video_name", "")).strip()
        if video_id and video_name:
            item["video_proxy_url"] = item.get("video_url", "")
            item["video_url"] = teldrive.file_url(c, video_id, video_name)
    total = db.get_recordings_total(camera=camera, date_from=date_from, date_to=date_to)
    
    data = {
        "success": True,
        "recordings": recordings,
        "total": total,
        "page": page,
        "limit": limit,
    }

    if c.get("redis_enabled"):
        import json
        try:
            redis_cache.set_cache(cache_key, json.dumps(data), 3600, c)
        except Exception:
            pass

    return data

@app.delete("/api/events")
def clear_events(
    camera: str | None = None,
    _: str = Depends(auth.require_auth),
):
    if camera == "" or camera == "All":
        camera = None
    deleted = db.clear_events(camera=camera, exclude_recordings=True)
    return {"success": True, "message": f"Deleted {deleted} events"}


@app.delete("/api/recordings")
def clear_recordings(
    camera: str | None = None,
    _: str = Depends(auth.require_auth),
):
    if camera == "" or camera == "All":
        camera = None
    deleted = db.clear_events(camera=camera, recordings_only=True)
    return {"success": True, "message": f"Deleted {deleted} recordings"}

@app.post("/api/capture")
def capture(_: str = Depends(auth.require_auth)):
    try:
        c = config.read_config()
        monitor.capture_snapshot(c)
        return {"success": True, "message": "Captured snapshot"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/camera/snapshot")
def get_camera_snapshot(index: int, refresh: bool = False, _: str = Depends(auth.require_auth)):
    try:
        return camera_snapshot_response(index, refresh)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/cameras/snapshot")
def get_cameras_snapshot(index: int, refresh: bool = False, _: str = Depends(auth.require_auth)):
    try:
        return camera_snapshot_response(index, refresh)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def camera_snapshot_response(index: int, refresh: bool = False) -> Response:
    c = config.read_config()
    camera = config.get_camera(c, index)
    if monitor.has_go2rtc_frame_source(c, camera):
        try:
            content, src = monitor.fetch_go2rtc_frame_bytes(c, camera, timeout=10, attempts=4)
            return Response(
                content=content,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "private, max-age=10, no-cache",
                    "X-Camera-Frame-Source": src,
                },
            )
        except Exception as exc:
            logger.warning("[SNAPSHOT] go2rtc thumbnail failed camera=%s: %s", camera.get("name", index), exc)

    path = monitor.camera_snapshot_path(index)
    if refresh or not path.exists():
        path = monitor.capture_camera_snapshot(c, index)
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=120"},
    )


@app.get("/api/camera/video")
def get_camera_video(index: int, _: str = Depends(auth.require_auth)):
    try:
        c = config.read_config()
        camera = config.get_camera(c, index)
        rtsp_url = str(camera.get("rtsp_url", "")).strip()
        if not rtsp_url:
            raise ValueError("No RTSP URL configured for this camera")
        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            monitor.mjpeg_frames(rtsp_url),
            media_type="multipart/x-mixed-replace; boundary=frame"
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/test-ai")
def test_ai(_: str = Depends(auth.require_auth)):
    try:
        c = config.read_config()
        path = monitor.SNAPSHOT_PATH
        if not path.exists():
            monitor.capture_snapshot(c, path)
        result, desc, raw = ai.verify_scene(path, c, camera=None)
        return {"success": True, "result": result, "description": desc, "raw": raw}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/test-ai-camera")
def test_ai_camera(index: int, _: str = Depends(auth.require_auth)):
    try:
        c = config.read_config()
        camera = config.get_camera(c, index)
        path = monitor.capture_camera_snapshot(c, index)
        result, desc, raw = ai.verify_scene(path, c, camera=camera)
        camera_name = str(config.get_camera(c, index).get("name", f"Camera {index}"))
        return {"success": True, "camera": camera_name, "result": result, "description": desc, "raw": raw}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/test-telegram")
def test_telegram(_: str = Depends(auth.require_auth)):
    try:
        c = config.read_config()
        path = monitor.SNAPSHOT_PATH
        if not path.exists():
            monitor.capture_snapshot(c, path)
        ai.send_telegram(path, "Test notification from Fall Detection Web", c)
        return {"success": True, "message": "Telegram message sent"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/test-ai-upload")
def test_ai_upload(file: UploadFile = File(...), _: str = Depends(auth.require_auth)):
    try:
        content = file.file.read()
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("File exceeds 10MB limit")
        
        test_path = DATA_DIR / "upload_test.jpg"
        test_path.write_bytes(content)
        
        c = config.read_config()
        result, desc, raw = ai.verify_scene(test_path, c, camera=None)
        return {"success": True, "result": result, "description": desc, "raw": raw}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
