# Counting Snapshots (ảnh lúc băng vạch) — Implementation Plan

> Mở rộng tính năng dual-counting: chụp ảnh mỗi lượt IN/OUT để soi 2 bộ đếm (Camera Axis vs YOLO) đúng/sai. Nhánh: `feat/dual-counting-test`.

**Goal:** Mỗi crossing được đếm → lưu 1 snapshot, gắn `events.snapshot_path`; trang chi tiết hiện "Log hôm nay" (giờ · nguồn · IN/OUT · thumbnail).

**Global constraints:**
- App fall_detection_web; PostgreSQL qua `db.get_conn()`; VN+7 today; auth trên mọi route mới.
- Ảnh lưu `data/counting_snaps/` (volume `fdw_data`, app serve). Tự prune.
- Regression-safe: không đụng query Phase-1; `snapshot_path` chỉ thêm dữ liệu.
- event_collector là service riêng (aiomqtt+asyncpg) — thêm httpx fetch go2rtc, KHÔNG đổi vai trò store-only quá mức.

---

## Task P1: Hạ tầng ảnh + YOLO snapshot + serve + log + UI

**Files:** `db.py`, `monitor.py`, `app.py`, `templates/camera_detail.html`.

### db.py
1. Thêm hằng + tạo dir trong `ensure_data_dir()`:
```python
COUNTING_SNAPS_DIR = DATA_DIR / "counting_snaps"
# trong ensure_data_dir():  COUNTING_SNAPS_DIR.mkdir(parents=True, exist_ok=True)
```
2. `insert_counting_event` thêm tham số `snapshot_path: str | None = None` và đưa vào cột `snapshot_path` của INSERT.
3. Hàm mới:
```python
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

def cleanup_counting_snaps(max_age_seconds: int = 2*86400) -> None:
    ensure_data_dir()
    cutoff = time.time() - max_age_seconds
    for p in COUNTING_SNAPS_DIR.glob("*.jpg"):
        try:
            if p.stat().st_mtime < cutoff: p.unlink()
        except OSError: continue
```

### monitor.py `_counting_loop`
Khi có `direction` (ngay trước khi `db.insert_counting_event(...)`), lưu frame hiện tại + truyền path:
```python
snap_path = None
try:
    db.COUNTING_SNAPS_DIR.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    fname = f"{now_utc.strftime('%Y%m%dT%H%M%S%f')}_yolo_{direction}.jpg"
    p = db.COUNTING_SNAPS_DIR / fname
    cv2.imwrite(str(p), frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    snap_path = str(p)
except Exception:
    pass
db.insert_counting_event(cam_id, direction, datetime.now(timezone.utc), "yolo",
                         track_id=str(tid), snapshot_path=snap_path)
```
(`cv2` đã import trong loop; dùng `frame` đang xử lý.)

### app.py
1. Serve route (path-validated như `/api/reid-crop`):
```python
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
```
2. Trong `_counting_blocks`, thêm khóa `"log"`:
```python
    ...
    "reset_ts": reset_ts_iso,
    "log": [{**x, "snap_url": (f"/api/counting-snap/{x['snap']}" if x["snap"] else None)}
            for x in db.counting_log_today(cam_id)],
```

### templates/camera_detail.html
1. Sau block reset (trong template `cameraMetrics`), thêm vùng log:
```html
        <div class="count-log full-width" id="countLog"></div>
```
2. CSS:
```css
    .count-log { grid-column: span 2; display:flex; flex-direction:column; gap:6px; max-height:320px; overflow-y:auto; }
    .count-log .row { display:flex; align-items:center; gap:10px; font-size:13px; padding:4px 0; border-bottom:1px solid var(--border); }
    .count-log img { width:64px; height:40px; object-fit:cover; border-radius:4px; cursor:pointer; }
    .count-log .src-axis { color:#378ADD; } .count-log .src-yolo { color:#EF9F27; }
    .count-log .dir-in { color: var(--good,#3fb950); } .count-log .dir-out { color: var(--bad,#f85149); }
```
3. Trong `loadCounting()`, sau khi set số, render log:
```javascript
        const log = (d.log || []).map(e => `<div class="row">
          <span style="width:64px">${e.snap_url ? `<img src="${e.snap_url}" onclick="window.open('${e.snap_url}')">` : "—"}</span>
          <span class="src-${e.source}" style="width:120px">${e.source === "yolo" ? "🤖 YOLO" : "📷 Camera"}</span>
          <span class="dir-${e.direction}" style="width:60px">${e.direction === "in" ? "VÀO" : "RA"}</span>
          <span style="color:var(--muted)">${e.time}</span></div>`).join("");
        const el = document.getElementById("countLog"); if (el) el.innerHTML = log || `<div style="color:var(--muted);font-size:12px">Chưa có lượt nào hôm nay.</div>`;
```

**Verify (docker harness + dev container `fdw_dev`):**
- Back-fill 1 test row có ảnh: copy 1 jpg vào volume `counting_snaps`, INSERT 1 `counter_yolo` với snapshot_path → GET `/api/counting/camera/cam_door` (authed) thấy `log` có entry + `snap_url`; GET `/api/counting-snap/<file>` (authed) trả 200 image; mở trang thấy thumbnail.
- Restart `fdw_dev`; reset không lỗi; trang load log.
- (Thật) bạn đi qua cửa → YOLO log +1 kèm ảnh.
- Commit: `feat(counting): snapshot YOLO lúc băng vạch + log ảnh trên trang chi tiết`.

---

## Task P2: Snapshot phía Camera (Axis) qua event_collector

**Files:** `services/event_collector/.../main.py`, `repo.py`, `requirements.txt`, `docker-compose.yml`; set `cameras.go2rtc_src` cho cam 1.

### compose (service event_collector)
Thêm:
```yaml
    environment:
      ... (giữ nguyên) ...
      GO2RTC_INTERNAL_URL: ${GO2RTC_INTERNAL_URL:-http://go2rtc:1984}
      COUNTING_SNAPS_DIR: /app/data/counting_snaps
    volumes:
      - fdw_data:/app/data
```

### requirements.txt (event_collector)
Thêm `httpx`.

### repo.py
```python
    async def go2rtc_src_for(self, cam_id: int) -> str | None:
        async with self.pool.acquire() as c:
            row = await c.fetchrow("SELECT go2rtc_src FROM cameras WHERE id=$1", cam_id)
            return (row["go2rtc_src"] or None) if row else None

    async def set_snapshot(self, event_id: int, path: str) -> None:
        async with self.pool.acquire() as c:
            await c.execute("UPDATE events SET snapshot_path=$1 WHERE id=$2", path, event_id)
```

### main.py
Thêm hàm fetch + lưu (httpx async), gọi sau khi `insert_counter` trả `ev_id`:
```python
import os, httpx
from datetime import datetime, timezone

async def _save_axis_snapshot(repo, cam_id, ev_id, direction):
    src = await repo.go2rtc_src_for(cam_id)
    base = os.environ.get("GO2RTC_INTERNAL_URL", "http://go2rtc:1984")
    snaps = os.environ.get("COUNTING_SNAPS_DIR", "/app/data/counting_snaps")
    if not src: return
    os.makedirs(snaps, exist_ok=True)
    url = f"{base.rstrip('/')}/api/frame.jpeg?src={src}"
    try:
        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(url)
        if r.status_code == 200 and r.content:
            ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            fname = f"{ts}_axis_{direction}.jpg"
            with open(os.path.join(snaps, fname), "wb") as f:
                f.write(r.content)
            await repo.set_snapshot(ev_id, os.path.join(snaps, fname))
    except Exception as exc:
        log.warning("axis_snapshot_failed", error=str(exc))
```
Trong `handle_message`, sau `if ev_id is not None:` (counter), gọi `await _save_axis_snapshot(repo, cam_id, ev_id, event["direction"])`.

### Set go2rtc_src cho cam 1
`UPDATE cameras SET go2rtc_src='cam_door' WHERE id=1` (để fetch frame go2rtc).

**Verify:**
- Rebuild event_collector image (`docker compose build event_collector`), up lại.
- Standalone: gọi `_save_axis_snapshot` logic bằng cách fetch `http://go2rtc:1984/api/frame.jpeg?src=cam_door` trong 1 container → 200 + ảnh lưu được vào volume.
- Thực: chỉ chạy khi camera Axis bắn counter event (hiện 0). Ghi rõ: KHÔNG verify được end-to-end tới khi Axis publish counter.
- Commit: `feat(event_collector): snapshot go2rtc lúc Axis bắn counter event`.
</content>
