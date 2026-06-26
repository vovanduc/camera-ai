# Design — Phase 1: Module ĐẾM người ra/vào

**Ngày:** 2026-06-26
**Trạng thái:** DONE (implemented — plan `../plans/2026-06-26-phase1-counting.md`, branch `feat/phase1-counting`; pipeline live-verified, chờ organic crossing chốt số)
**Phase:** 1 / 5 (xem tổng thể: `2026-06-26-dcnet-platform-migration-design.md`)
**Tiền đề:** Phase 0 DONE + merged — FDW chạy Postgres (`incidents`/`users`/`settings`), `docker-compose.yml` (postgres pgvector/pg16 + fall_detection_web), psycopg sync.
**Nguồn port:** repo `dcnet-cloud/camera` (`services/event_collector/`, `services/dashboard/.../counting.py` + `app.py`, `db/init.sql`).

## 1. Mục tiêu

Đưa logic ĐẾM người ra/vào (đã chạy prod ở DCNET) vào camera-ai: cam Axis phát crossline IN/OUT qua MQTT → `event_collector` ghi `events` vào Postgres chung → FDW hiển thị **occupancy "đang trong phòng" + IN/OUT hôm nay + log + chart theo giờ** (core parity Streamlit). Sau Phase này camera-ai đếm được như DCNET hiện tại.

**Không thuộc scope:** filter khoảng ngày + chart theo ngày + multi-cam UI (defer); Re-ID/group (Phase 2); modular per-customer (Phase 3); cutover prod (Phase 4).

## 2. Quyết định đã chốt (review 2026-06-26)

1. **Bảng `cameras` riêng + seed cam Axis.** Thêm bảng `cameras` của DCNET (FK cho `events.cam_id`); seed cam Axis `B8A44F4627CE`. FDW fall-detection camera-config vẫn ở `settings`-JSON. 2 registry tạm thời → gộp ở Phase 3.
2. **UI = core parity.** 1 trang "Đếm ra/vào": occupancy + IN/OUT hôm nay + log crossing gần nhất + chart theo giờ (`bucket_hourly`). Auto-refresh polling.
3. **Broker = cloud đọc ké, client-id riêng.** `event_collector` nối `camera-test.dcnet.vn:8883` TLS, `MQTT_CLIENT_ID=event_collector_cameraai` (DUY NHẤT — ko kick collector DCNET prod). Data crossing thật chảy. End goal: deploy thay prod cũ (Phase 4).

## 3. Kiến trúc & dataflow

```
Cam Axis (ACAP OA crossline) ─MQTT/TLS (axis/#)─► cloud broker camera-test.dcnet.vn:8883
        │
        ▼
event_collector (service riêng, async aiomqtt+asyncpg) — parse topic→direction, INSERT events (store-only, idempotent)
        │
        ▼
PostgreSQL (chung Phase 0) — bảng cameras + events (crossing)
        ▲
        │ sync psycopg (db.py)
FDW app "Đếm ra/vào" page: occupancy = COUNT(IN today) − COUNT(OUT today); hourly = bucket_hourly; log = events gần nhất
```

**Topology:** `event_collector` = service async riêng (tách ingest khỏi web → web restart ko mất event). FDW app sync psycopg chỉ ĐỌC để hiển thị. 2 style DB (async collector / sync web) chung schema — đã chấp nhận ở migration spec.

## 4. Schema (thêm vào Postgres Phase 0)

Port từ DCNET `db/init.sql` (chỉ 2 bảng cần cho counting):

```sql
CREATE TABLE cameras (
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

CREATE TABLE events (
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
CREATE INDEX events_cam_ts ON events (cam_id, ts DESC);
CREATE INDEX events_type_ts ON events (type, ts DESC);
```

- Tên `events` giờ TRỐNG (Phase 0 đã đổi FDW `events`→`incidents`) → coexist sạch với `incidents`.
- Seed: `INSERT INTO cameras (cam_uid,name,rtsp_url,model,location) VALUES ('B8A44F4627CE','Cửa cty HCM','rtsp://...','M3216-LVE','HCM') ON CONFLICT DO NOTHING;`
- **`events` (crossing) sống ở cả collector (asyncpg ghi) lẫn FDW (psycopg đọc)** — chỉ chung bảng, ko chung code.

## 5. Components & files

**a) `services/event_collector/` (MỚI — port từ DCNET, gần verbatim)**
- `src/event_collector/{__init__,parser,repo,main}.py` + `Dockerfile` + `requirements.txt` (asyncpg, aiomqtt, structlog).
- `parser.py`: Axis MQTT topic → normalized event dict (direction IN/OUT theo Scenario1/2, loại Interval/Passthrough). Port nguyên.
- `repo.py`: `insert_counter` (INSERT-only, idempotent) + `cam_id_for(cam_uid)`. Port nguyên (asyncpg).
- `main.py`: MQTT consume loop + dispatcher store-only. Port; đổi `MQTT_CLIENT_ID` default → `event_collector_cameraai`.
- DSN env chung Postgres camera-ai; MQTT env cloud broker TLS.

**b) `fall_detection_web/counting.py` (MỚI — port pure stdlib)**
- `bucket_hourly(crossings, day)`, `summarize(crossings)` (+ `bucket_daily` nếu rẻ). VN+7. Port nguyên từ DCNET — pure, không I/O.

**c) `fall_detection_web/db.py` (thêm hàm counting, sync psycopg)**
- `counting_occupancy_today(cam_id=None) -> {in,out,occupancy}`.
- `counting_crossings(day) -> list[dict]` (cho bucket_hourly + log).
- `list_cameras()` / `cam_id_for(cam_uid)`.
- Giữ pattern psycopg dict_row + pool hiện có. KHÔNG đổi hàm cũ.

**d) `fall_detection_web/app.py` + `templates/counting.html`**
- Route `GET /counting` (JWT auth như route khác) render trang: 3 số (occupancy/IN/OUT hôm nay) + chart giờ + log. API `GET /api/counting` trả JSON cho polling auto-refresh (theo pattern fetch hiện có của FDW).
- Thêm link "Đếm ra/vào" vào nav.

**e) `docker-compose.yml`: thêm service `event_collector`** (build ./services/event_collector, env_file, depends_on postgres healthy, restart unless-stopped).

## 6. Error handling
- event_collector: reconnect MQTT (retry loop), idempotent INSERT (ON CONFLICT DO NOTHING) → reboot/duplicate ko tạo row giả. Motion/health topic bỏ qua. (Port hành vi DCNET đã verified.)
- FDW counting query: cam ko có event → trả 0/empty, ko lỗi. occupancy âm (lệch IN/OUT) → hiển thị max(0, …) hoặc raw + note (theo DCNET).

## 7. Testing
- `counting.py`: port `test_counting.py` DCNET (pure unit — bucket_hourly/summarize/occupancy logic). Chạy `pytest`.
- `event_collector/parser.py`: unit test topic→direction (IN/OUT, loại Interval).
- Live verify: collector nối cloud broker → đi qua cửa (hoặc data sẵn) → `events` có row → trang /counting hiện occupancy/IN/OUT/chart/log đúng. psql đối chiếu COUNT.
- FDW vẫn ko có pytest cho web layer → verify UI bằng chạy app (login → /counting render + /api/counting JSON).

## 8. Decompose (cho writing-plans)
1. Schema counting (cameras+events) + seed Axis + init trong db.py/migration.
2. `event_collector` service (port 3 file + Dockerfile + requirements + compose service) + parser test.
3. `counting.py` (port) + db.py counting queries + unit tests.
4. UI: app.py route `/counting` + `/api/counting` + `templates/counting.html` + nav link.

## 9. Open (confirm khi review)
- Occupancy reset/âm: hiển thị `max(0,...)` hay raw? → đề xuất raw + ko cho âm (clamp 0) như DCNET.
- Seed camera: hardcode cam Axis trong migration SQL hay seed script riêng? → đề xuất SQL seed idempotent trong init.

---
## Liên quan
- Tổng thể: [migration design](2026-06-26-dcnet-platform-migration-design.md) · Plan: chưa có (writing-plans khi implement)
- Trước: Phase 0 (Unify DB — merged, plan `../plans/2026-06-26-phase0-unify-db-postgres.md`) · Sau: [Phase 2 Group/Re-ID](2026-06-26-phase2-group-reid-design.md)
