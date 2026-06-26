# Design — Phase 3: Modular per-customer

**Ngày:** 2026-06-26
**Trạng thái:** DONE (implemented — LIGHT scope; plan `../plans/2026-06-26-phase3-modular-percustomer.md`, branch `feat/phase3-modular`)

---

## ⚠️ Thực tế (audit 2026-06-26) — spec gốc viết BLIND, đọc phần này trước

Spec gốc (dưới) viết khi chưa đọc `monitor.py`/`config.py`/`cameras.html`/`app.py`/`reid_worker`. Audit khi implement phát hiện:

1. **2 registry DISJOINT schema:** FDW cameras = JSON blob trong `settings` (13 field per-cam: name, rtsp_url, go2rtc_src, live_url, live_mode, prompt_id, local_save_*, teldrive_*, record_* — **đều per-camera**, KHÔNG global như §2.6 giả định). Bảng `cameras` (counting/reid) lean, không có các field đó. Gộp = bloat ~10 cột hoặc hybrid JSONB.
2. **FDW cameras UI = JS SPA** (`/cameras` fetch `/api/cameras`, POST full array, `<dialog>`, optimistic toggle) — KHÔNG server-rendered form. Spec §5.d/§5.e `POST /cameras/{id}/modules` form **sai pattern**.
3. **`reid_worker` = single-cam-per-container** (`CAM_UID` env → `cam_id_for`), KHÔNG list-query. §5.g list-query rewrite không cần (design sạch, shelved).
4. **settings-JSON cameras RỖNG** (greenfield, đã verify) → migration (§2.4/§4.3/§5.h) migrate KHÔNG GÌ.
5. **"Axis YOLO off" goal ĐÃ đạt sẵn:** monitor.py đọc settings-JSON cameras (rỗng) → không chạy YOLO; cam Axis ở bảng `cameras` (monitor không thấy). Registry tách biệt → no double-process.

### Scope ĐÃ implement (LIGHT — disjoint camera sets + greenfield):
- 4 cột flag (`counting/fall_detection/reid/live_enabled`) trên bảng `cameras` + 2 partial index + seed Axis (counting+live) — §4.1/§4.2 ✅
- db helpers `list_cameras_for_module`/`list_cameras_all`/`update_camera_modules` — §5.a (subset) ✅
- counting page filter `counting_enabled` (JOIN cameras) ✅
- trang `/modules` toggle UI (SPA fetch, bảng cameras) + `/api/camera-modules` GET/POST ✅

### Lưu ý vận hành:
- **Auto-register default:** camera được `event_collector` tự đăng ký từ MQTT topic sẽ có `enabled=true` nhưng `counting_enabled=false` — các crossing của cam đó bị loại khỏi JOIN đếm một cách im lặng cho đến khi toggle bật tại `/modules`.

### DEFERRED (wire khi có deploy mixed multi-customer thật — disjoint + greenfield nên chưa cần):
- §2.4/§2.5/§4.3/§5.h settings-JSON ↔ cameras-table merge + migration script (không có data).
- §5.b monitor.py rewire (`cfg["cameras"]` → DB query) — đòi registry merge trước.
- §5.c config.py xóa `cameras` key — đòi merge.
- §5.g reid_worker flag-gate — shelved; `reid_enabled` là gate check KHI activate, không sửa code shelved.
- §2.6 per-cam config override — N/A.

Phần dưới = spec gốc (giữ để tham chiếu; mục bị defer đã liệt kê trên).

---
**Phase:** 3 / 5 (xem tổng thể: `2026-06-26-dcnet-platform-migration-design.md`)
**Tiền đề:** Phase 0 DONE (Postgres, `incidents`/`users`/`settings`); Phase 1 DONE (bảng `cameras` + `events`, `event_collector`, UI đếm); Phase 2 DONE (Re-ID `face_vectors`/`reid_groups`, `reid_worker`, page Nhóm theo người).
**Vấn đề cốt lõi:** Sau Phase 1 tồn tại **hai camera registry** song song: bảng `cameras` (counting ACAP) và `settings`-JSON key `"cameras"` (FDW fall-detection). Phase 3 **gộp về một** source of truth, bổ sung module flag per-camera, wire flag vào mỗi service.

---

## 1. Mục tiêu

Biến mỗi camera thành **tập hợp module bật/tắt độc lập** — `counting_enabled / fall_detection_enabled / reid_enabled / live_enabled` — để một camera Axis có ACAP edge detection **tắt hoàn toàn YOLO** (không detect lại trên server), trong khi camera IP thường (không ACAP) dùng YOLO bình thường. Cụ thể:

1. **Unify camera registry:** gộp FDW `settings`-JSON `cameras` vào bảng `cameras` (Phase 1) làm single source of truth; sau Phase 3 key `cameras` không còn trong `settings`.
2. **Module flag per-camera:** thêm 4 cột boolean `counting_enabled / fall_detection_enabled / reid_enabled / live_enabled` vào bảng `cameras`.
3. **Wire flag vào service:** `monitor.py` chỉ chạy YOLO cho cam `fall_detection_enabled=true`; `event_collector` chỉ ingest `counting_enabled` cam (qua `cam_uid`); `reid_worker` chỉ xử lý `reid_enabled` cam; UI chỉ hiện module được bật.
4. **Per-camera config UI:** trang cameras-management (đã có trong FDW) cho phép toggle từng module.

**Không thuộc scope:** rules engine phức tạp (permissions/role-based feature access); per-camera override của global detection params (confidence/yolo_model) — giữ global trong `settings`; bật lại Re-ID inference (gate NO-GO, pending đặt lại cam); cutover prod (Phase 4).

---

## 2. Quyết định & giả định

### 2.1 Module flags — boolean columns (không JSONB)

Chọn **4 cột `BOOLEAN DEFAULT false`** thêm thẳng vào bảng `cameras`:

```
counting_enabled       BOOLEAN NOT NULL DEFAULT false
fall_detection_enabled BOOLEAN NOT NULL DEFAULT false
reid_enabled           BOOLEAN NOT NULL DEFAULT false
live_enabled           BOOLEAN NOT NULL DEFAULT false
```

**Lý do chọn columns thay JSONB:**
- Indexable: `WHERE fall_detection_enabled = true` dùng được index (monitor chạy query này nhiều lần/giây).
- Khớp schema flat hiện tại của `cameras` (Phase 1) — không cần introduce JSONB semantics.
- 4 module đã biết, POC ethos: không over-engineer cho module growth chưa tồn tại.
- Nếu sau này module > 6–8: refactor sang JSONB `modules` — rõ ràng hơn là thêm 10 cột.

**JSONB** vẫn là lựa chọn tốt khi module list mở rộng nhanh hoặc cần metadata per-module (threshold, params riêng). Defer sang Phase 4/5.

### 2.2 Quan hệ `enabled` vs module flags

Cột `enabled` (Phase 1) = **camera master switch**: khi `enabled=false`, camera bị tắt hoàn toàn — mọi module đều dừng bất kể flag riêng.

Module flag = **feature bật trên camera đang active**. Logic check ở mỗi service:

```
camera chạy module X ⟺ cameras.enabled = true AND cameras.X_enabled = true
```

Không có trường hợp nào `enabled=false` nhưng module flag vẫn có hiệu lực.

### 2.3 Matrix module cam mẫu (seed state)

| cam_uid | name | enabled | counting | fall_det | reid | live | Ghi chú |
|---|---|---|---|---|---|---|---|
| `B8A44F4627CE` | Cửa cty HCM | true | **true** | false | false | true | ACAP OA → YOLO off; counting qua MQTT |
| *(FDW cam tên slug)* | *(cam fall-det cũ)* | true | false | **true** | false | true | camera thường → YOLO on |

Seed idempotent `ON CONFLICT DO NOTHING` hoặc `DO UPDATE` khi migrate từ `settings`-JSON.

### 2.4 Migration settings-JSON cameras → bảng `cameras`

Greenfield stance (ratified Phase 0 P0.5/P0.10): **optional, không mandatory**.

Lý do: FDW chưa có deployment thật với data cần giữ → bảng `cameras` có thể tạo mới bằng seed script. Script `migrate_fdw_cameras.py` được thiết kế idempotent (`ON CONFLICT (cam_uid) DO NOTHING`) nhưng **KHÔNG chạy bắt buộc** nếu environment là greenfield.

**Vấn đề mapping `cam_uid`:** FDW `settings`-JSON cameras được keyed bởi name/rtsp_url, không có hardware UID. Migration cần sinh cam_uid:
- Nếu camera là Axis (có serial trong rtsp_url hoặc config): dùng serial hex làm cam_uid.
- Fallback: `slug(name)` (lowercase, replace spaces → `_`, chặt 32 ký tự) — duy nhất trong tenant, đủ cho POC.
- Flag là **Open item** (xem §9).

### 2.5 Key `cameras` rời khỏi `settings`

Sau Phase 3, key `"cameras"` trong bảng `settings` / `DEFAULT_CONFIG` / `ENV_CONFIG_KEYS` không còn dùng:
- `config.py`: xoá `"cameras": []` khỏi `DEFAULT_CONFIG`. `CAMERAS` **không có** trong `ENV_CONFIG_KEYS` (confirmed từ config.py lines 81–111) → không cần xoá ENV mapping. Chỉ xoá DEFAULT_CONFIG entry.
- `db.py`: hàm mới `list_cameras_for_module(module: str) -> list[dict]` thay thế đọc cameras từ settings.
- `monitor.py`: thay `cfg["cameras"]` bằng query DB `list_cameras_for_module("fall_detection")`.

**Cảnh báo:** Chưa trace toàn bộ caller `cfg["cameras"]` trong `app.py`/`monitor.py`/templates vì không đọc các file đó (scope constraint). **Audit toàn bộ reader key `cameras` từ settings khi implementation** — xem §9.

### 2.6 Per-camera tuning config — giữ global

FDW hiện có global detection params trong `settings`: `confidence`, `yolo_model`, `yolo_imgsz`, `verify_prompt`, v.v. Phase 3 **không** thêm per-camera override cho các params này — giữ global trong `settings` (POC ethos). Nếu khách cần per-cam confidence khác nhau: defer Phase 4/5, implement per-cam `config` JSONB column.

---

## 3. Kiến trúc & dataflow

```
┌──────────────────────────────────────────────────────────────────────┐
│                   Postgres — bảng cameras (unified)                  │
│  id | cam_uid | rtsp_url | enabled | counting | fall_det | reid | live│
└──────────────┬──────────────────────────────────────┬────────────────┘
               │                                      │
               ▼ query WHERE fall_detection_enabled   ▼ startup cam_uid lookup (store-all)
        monitor.py (FDW)                       event_collector (async)
        YOLO thread per cam FD=true            parse MQTT axis/# → INSERT events
        (skip cam FD=false: Axis ACAP)         (cam_uid→cam_id FK; store tất cả events)
               │                                      │
               ▼                                      ▼
       incidents table                          events table
               │                                      │
               └─────────────┬────────────────────────┘
                             ▼
                    FDW app (FastAPI + Jinja)
                    /cameras → toggle module UI (list_cameras_all)
                    /counting → JOIN cameras WHERE counting_enabled (filter query)
                    /fall-detection → chỉ hiện cam fall_det_enabled
                    /live → list_cameras_for_module("live")
                    /reid → chỉ cam reid_enabled (Phase 2+)
```

**reid_worker** (Phase 2, optional service, off mặc định): startup query `WHERE reid_enabled=true AND enabled=true` → chỉ enqueue snapshot task cho cam đó.

**Không có runtime toggle:** flag đọc khi startup/reload, không hot-swap. Thay đổi flag → restart service liên quan (acceptable POC). Nếu cần hot-swap: defer (cần signal/reload loop).

---

## 4. Schema — cameras table extension + migration

### 4.1 ALTER TABLE cameras (thêm 4 cột)

```sql
-- Phase 3 migration: thêm module flags vào cameras
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS counting_enabled       BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS fall_detection_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS reid_enabled           BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS live_enabled           BOOLEAN NOT NULL DEFAULT false;

-- Index để service query nhanh (monitor chạy thường xuyên)
CREATE INDEX IF NOT EXISTS cameras_fall_det ON cameras (fall_detection_enabled) WHERE enabled = true;
CREATE INDEX IF NOT EXISTS cameras_counting  ON cameras (counting_enabled)       WHERE enabled = true;
```

Thực thi trong `db.py`/`init_db` hoặc migration script riêng — xem §5.c.

### 4.2 Seed update cam Axis (Phase 1 seed đã có row, cần update flags)

```sql
UPDATE cameras
   SET counting_enabled = true,
       live_enabled     = true
 WHERE cam_uid = 'B8A44F4627CE';  -- idempotent: SET to true is safe to re-run
```

### 4.3 Migration settings-JSON cameras → cameras table (optional)

Script `scripts/migrate_fdw_cameras.py`:

```python
# Đọc settings.value WHERE key='cameras' → parse JSON list
# Mỗi cam dict: sinh cam_uid (serial từ rtsp_url hoặc slug(name))
# INSERT INTO cameras (..., fall_detection_enabled=true, live_enabled=true)
#   ON CONFLICT (cam_uid) DO NOTHING
# Log summary: inserted / skipped
```

Chạy một lần, idempotent. **Optional** nếu greenfield (không có data FDW cần giữ).

Sau khi verify rows khớp: xoá key `cameras` khỏi `settings` table:
```sql
DELETE FROM settings WHERE key = 'cameras';
```

### 4.4 Full cameras schema (sau Phase 3)

```sql
CREATE TABLE cameras (
    id                     SERIAL PRIMARY KEY,
    cam_uid                TEXT UNIQUE NOT NULL,
    name                   TEXT NOT NULL,
    rtsp_url               TEXT NOT NULL,
    mjpeg_url              TEXT,
    vendor                 TEXT DEFAULT 'axis',
    model                  TEXT,
    location               TEXT,
    enabled                BOOLEAN DEFAULT true,
    -- Phase 3: module flags
    counting_enabled       BOOLEAN NOT NULL DEFAULT false,
    fall_detection_enabled BOOLEAN NOT NULL DEFAULT false,
    reid_enabled           BOOLEAN NOT NULL DEFAULT false,
    live_enabled           BOOLEAN NOT NULL DEFAULT false,
    created_at             TIMESTAMPTZ DEFAULT now()
);
```

---

## 5. Components & files

### a) `fall_detection_web/db.py` — thêm hàm module-aware

Thêm hàm mới, giữ nguyên hàm cũ (backward compat):

- `list_cameras_for_module(module: str) -> list[dict]`
  - `module` ∈ `{"fall_detection", "counting", "reid", "live"}`
  - Query: `SELECT * FROM cameras WHERE enabled=true AND {module}_enabled=true ORDER BY id`
  - Dùng cho monitor.py, reid_worker, counting page.
- `list_cameras_all() -> list[dict]` — cho trang cameras-management (hiện tất cả, kể cả disabled).
- `update_camera_modules(cam_id: int, modules: dict[str, bool]) -> None`
  - UPDATE cameras SET counting_enabled=%s, fall_detection_enabled=%s, ... WHERE id=%s
  - Dùng cho route toggle UI.
- `upsert_camera(cam_uid, name, rtsp_url, *, vendor, model, location, modules: dict) -> int`
  - INSERT ON CONFLICT (cam_uid) DO UPDATE — dùng cho migration script + seed.

Xoá (hoặc deprecate) hàm đọc cameras từ `settings` nếu có trong db.py hiện tại.

### b) `fall_detection_web/monitor.py` — wire fall_detection flag

**Thay** cách load cameras từ `cfg["cameras"]` (settings-JSON) bằng:

```python
from fall_detection_web.db import list_cameras_for_module
active_cams = list_cameras_for_module("fall_detection")
```

Startup: chỉ spawn YOLO thread cho cam trong `active_cams`. Cam Axis `B8A44F4627CE` (`fall_detection_enabled=false`) sẽ không có thread → tiết kiệm GPU/CPU.

**Lưu ý:** không read monitor.py trong scope này — **audit caller `cfg["cameras"]` khi implement** để đảm bảo không còn chỗ nào lấy cameras từ settings.

### c) `fall_detection_web/config.py` — xoá key `cameras`

- Xoá `"cameras": []` khỏi `DEFAULT_CONFIG`. `CAMERAS` không có trong `ENV_CONFIG_KEYS` (confirmed) → không cần xoá ENV mapping.
- Thêm comment: `# cameras now in DB cameras table (Phase 3) — use db.list_cameras_for_module()`.
- Numeric/boolean coercion không ảnh hưởng (cameras không phải numeric key).

### d) `fall_detection_web/app.py` — extend cameras management routes

Cameras management đã có trong FDW (routes + `cameras.html`/`camera_detail.html`). Phase 3 extend:

- `GET /cameras` — render tất cả cameras (dùng `list_cameras_all()`), hiển thị module flags dưới dạng toggle per camera.
- `POST /cameras/{id}/modules` — nhận form data `{counting_enabled, fall_detection_enabled, reid_enabled, live_enabled}` (checkbox), gọi `update_camera_modules(id, modules)`, redirect `/cameras`.
- `GET /cameras/{id}` (camera_detail) — hiện module flags hiện tại.
- Thêm `POST /cameras` (upsert) — không bắt buộc Phase 3 nếu đã có.

**Wire `live_enabled` vào live-view route:** route hiện tại hiển thị camera stream dùng `cfg["cameras"]` hoặc `rtsp_url` single cam. Sau Phase 3: gọi `list_cameras_for_module("live")` → chỉ hiển thị cam `live_enabled=true` trong dropdown live view. Cam `live_enabled=false` không xuất hiện trong UI live (hidden, không error).

**Không đổi** business logic routes counting/fall-detection/reid — chúng chỉ cần update nguồn danh sách cam từ `list_cameras_for_module(...)` qua `db.py`.

### e) `fall_detection_web/templates/` — cameras.html + camera_detail.html

- Thêm 4 toggle checkbox (hoặc switch UI) cho mỗi cam: Đếm / Fall-detection / Re-ID / Live view.
- Submit form `POST /cameras/{id}/modules`.
- Hiển thị badge module status trong danh sách cameras (color-coded: bật = green, tắt = grey).
- Giữ nguyên UI pattern hiện có (Jinja, Bootstrap hoặc class CSS FDW đang dùng — không audit templates scope này).

### f) `services/event_collector/src/event_collector/repo.py` — tùy chọn wire counting flag

Hiện tại `event_collector` INSERT events theo cam_uid lookup. Sau Phase 3:

- `cam_id_for(cam_uid)` có thể check `counting_enabled=true` — trả `None` nếu cam không có flag → bỏ qua event.
- **Alternative (đơn giản hơn):** giữ event_collector store-all, filter ở query tầng dashboard. Phù hợp POC ethos hơn (không mất event data, dễ debug).
- **Quyết:** Phase 3 chọn **filter ở query** (dashboard page lọc `JOIN cameras WHERE counting_enabled`); event_collector vẫn store-all. Nếu cần filter ingest: Phase 4.

### g) `services/reid_worker/` — wire reid flag

Startup: query `list_cameras_for_module("reid")` (asyncpg) → chỉ listen object_snapshot cho cam reid-enabled. Cam `B8A44F4627CE` hiện `reid_enabled=false` (gate NO-GO). Khi đặt lại cam/môi trường khách: toggle trong UI → restart reid_worker.

### h) `scripts/migrate_fdw_cameras.py` (MỚI, optional)

One-time migration script (chạy bằng tay):
```
python scripts/migrate_fdw_cameras.py --db-url $DATABASE_URL [--dry-run]
```
Output: log số cam migrated/skipped/conflict. Idempotent. Sau verify: script in câu SQL `DELETE FROM settings WHERE key='cameras'` để admin tự chạy (không auto-delete).

### i) `db/` — migration SQL (alter cameras)

File `db/migrations/003_phase3_module_flags.sql`:
```sql
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS counting_enabled       BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS fall_detection_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS reid_enabled           BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS live_enabled           BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS cameras_fall_det ON cameras (fall_detection_enabled) WHERE enabled = true;
CREATE INDEX IF NOT EXISTS cameras_counting  ON cameras (counting_enabled)       WHERE enabled = true;
-- Seed Axis cam module flags
UPDATE cameras SET counting_enabled=true, live_enabled=true WHERE cam_uid='B8A44F4627CE';
```

Không dùng Alembic (nhất quán POC — migration script thủ công như DCNET `db/init.sql`).

---

## 6. Error handling

- **Module flag load fail (DB down):** `list_cameras_for_module` ném exception → monitor.py bắt, log lỗi, retry sau interval (không crash service). reid_worker: tương tự retry loop đã có.
- **Cam mới thêm vào DB trong khi service đang chạy:** flag đọc lúc startup, không hot-reload — cam mới có hiệu lực sau restart service. Không cần handle runtime add.
- **settings-JSON cameras vẫn còn trong DB (migration chưa chạy):** `monitor.py` đã dùng `list_cameras_for_module` → không đọc settings cameras nữa → không bị double-process. Nếu migration chưa chạy và bảng `cameras` chưa có cam FD → monitor không có camera → log warning rõ "no cameras with fall_detection_enabled" thay vì silent fail.
- **Xóa key `cameras` từ settings:** chỉ sau verify migration xong. Config 3-tier không còn key `cameras` → `get_config("cameras")` trả `[]` (DEFAULT đã xoá) hoặc key error → caller phải đã được update trước. Phải **audit và update tất cả caller trước khi xoá** settings key.
- **cam_uid conflict trong migration:** `ON CONFLICT DO NOTHING` → log dòng nào skipped. Admin kiểm tra bằng tay.

---

## 7. Testing

- **Unit test `db.py` hàm mới:** `list_cameras_for_module` với fixture DB (psycopg test pool / mock cursor) — verify filter `enabled AND X_enabled`; verify `update_camera_modules` UPDATE đúng columns.
- **Integration verify (chạy app):**
  1. `ALTER TABLE` chạy thành công: `\d cameras` trong psql thấy 4 cột mới.
  2. Toggle `fall_detection_enabled=false` cho cam Axis → restart monitor → monitor log "0 cameras with fall_detection_enabled=true" (không spawn YOLO thread).
  3. Toggle `fall_detection_enabled=true` cho cam thường → restart → monitor spawn thread.
  4. UI `/cameras` hiển thị 4 toggle; submit form → DB update; reload → state persist.
  5. Migration script (`--dry-run`) log đúng số cam từ settings-JSON → DB.
  6. Sau migrate + delete settings key → `get_config("cameras")` không còn trả camera list cũ.
- **Re-ID wire:** `reid_worker` startup log "cameras with reid_enabled: 0" (cam Axis gate NO-GO).
- **Regression:** counting page (`/counting`) vẫn hiện occupancy/IN/OUT (query JOIN cameras WHERE counting_enabled không break query Phase 1).
- FDW vẫn không có pytest web layer → verify bằng chạy app + psql check.

---

## 8. Decompose (cho writing-plans)

1. **Schema migration:** `db/migrations/003_phase3_module_flags.sql` (ALTER + index + seed UPDATE Axis cam) + apply trong `init_db` hoặc migrate script.
2. **`db.py` hàm mới:** `list_cameras_for_module`, `list_cameras_all`, `update_camera_modules`, `upsert_camera` + unit tests.
3. **`monitor.py` wire flag:** thay `cfg["cameras"]` → `list_cameras_for_module("fall_detection")`. Audit và xoá mọi ref cũ.
4. **`config.py` cleanup:** xoá `"cameras"` khỏi `DEFAULT_CONFIG` + audit `ENV_CONFIG_KEYS`. Đảm bảo không còn caller nào dùng `cfg["cameras"]` trước khi xoá.
5. **`app.py` + templates:** route `POST /cameras/{id}/modules` + extend cameras.html/camera_detail.html với module toggles.
6. **`reid_worker` wire flag:** thay hardcoded cam list → query `list_cameras_for_module("reid")`.
7. **Migration script `migrate_fdw_cameras.py`** (optional): dry-run + live + verify.
8. **Verify end-to-end:** matrix cam test (cam Axis FD=false → no YOLO; cam thường FD=true → YOLO on; counting cam → events ingest + page đúng).

---

## 9. Open (confirm khi review)

| # | Vấn đề | Đề xuất / trạng thái |
|---|---|---|
| O1 | ~~`CAMERAS` có trong `ENV_CONFIG_KEYS` không?~~ | **CLOSED (confirmed):** `CAMERAS` không có trong `ENV_CONFIG_KEYS` (config.py lines 81–111). Chỉ xoá DEFAULT_CONFIG `cameras` key; không cần xoá ENV mapping. |
| O2 | FDW `settings`-JSON cameras field shape chính xác? | Chưa đọc `cameras.html`/`app.py` (scope constraint). **Audit khi implement** để map đúng fields → `cameras` columns. Đặc biệt: rtsp_url, name, và bất kỳ per-cam config nào (confidence override?). |
| O3 | cam_uid synthesis cho FDW cameras | Đề xuất: `slug(name)` = lowercase + replace space→`_`, giới hạn 32 ký tự, hoặc serial nếu rút được từ rtsp_url/config. **Confirm logic slug và xử lý collision** khi review. |
| O4 | monitor.py load cameras: startup-only hay periodic reload? | Đề xuất startup-only (restart để apply). Nếu cần hot-reload: thêm signal handler SIGHUP hoặc polling interval. Confirm với team. |
| O5 | Có per-cam config riêng trong FDW cameras-JSON không? (vd confidence per-cam) | Nếu có: cần per-cam `config` JSONB column (thêm vào schema Phase 3) hoặc migrate vào `settings` global. **Confirm khi audit cameras.html/app.py**. |
| O6 | Counting page filter: JOIN WHERE counting_enabled hay filter ở app layer? | Đề xuất JOIN DB (cleaner, consistent). Confirm không break Phase 1 query signatures (counting.py pure, db.py queries). |
| O7 | Module UI: checkbox form hay toggle/switch JS? | Đề xuất form checkbox đơn giản (no JS dependency) — consistent POC. Nếu FDW template đã dùng JS toggle pattern: match. Confirm khi xem template. |
| O8 | reid_worker Phase 2: đã implement asyncpg camera query chưa? | Nếu có hardcoded cam_uid trong reid_worker code: Phase 3 task #6 replace bằng DB query. Confirm khi đọc reid_worker code. |

---
## Liên quan
- Tổng thể: [migration design](2026-06-26-dcnet-platform-migration-design.md)
- Trước: [Phase 2 Group/Re-ID](2026-06-26-phase2-group-reid-design.md) · Sau: [Phase 4 Deploy](2026-06-26-phase4-deploy-cutover-design.md)
