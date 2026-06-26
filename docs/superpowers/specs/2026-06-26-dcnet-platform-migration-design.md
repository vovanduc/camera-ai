# Design — Di chuyển DCNET Camera Platform vào camera-ai (Unified Product)

**Ngày:** 2026-06-26
**Trạng thái:** DESIGN (chờ review)
**Phạm vi spec này:** Tổng thể migration (vision + phasing) + **chi tiết Phase 0 (Unify DB → Postgres)**. Các phase sau có spec riêng.
**Repo nguồn:** `dcnet-cloud/camera` (counting prod + Re-ID, Postgres/async/Streamlit).
**Repo đích (home lâu dài):** `camera-ai` (monorepo HA add-ons; `fall_detection_web` = FastAPI + Jinja UI + go2rtc + JWT + SQLite).

---

## 1. Mục tiêu & vision

Biến **camera-ai/`fall_detection_web` (FDW)** thành **sản phẩm hợp nhất (unified product)** của DCNET: nền UI/auth/live-view sẵn có của FDW + đổ logic DCNET (đếm IN/OUT, occupancy, group/Re-ID) vào, **mỗi tính năng = 1 module bật/tắt theo use-case từng khách**.

Nguyên tắc:
- **Module hoá:** mỗi cam/khách bật module cần (counting / fall-detection / reid / live). Vd cam Axis có ACAP edge detect người → **YOLO off** (không cần detect lại).
- **DB thống nhất = PostgreSQL.** Gom các bảng SQLite của FDW + schema counting/Re-ID của DCNET vào 1 Postgres. Phát triển lâu dài 1 DB.
- **Không gãy prod:** counting Streamlit hiện đang live → giữ chạy tới khi camera-ai đạt parity rồi mới cut over.

**Không thuộc scope (defer):** tính năng LLM (NL query text-to-SQL, narrate báo cáo) = track riêng sau; bật lại Re-ID inference cần đặt lại cam (xem repo `camera`: `docs/reid-capture-findings.md`).

## 2. Hướng đã chọn: ① Port DCNET → vào camera-ai

camera-ai/FDW làm nền (giữ UI/auth/live/VLM/Telegram/go2rtc), đổ counting + group + Postgres vào.
- ✅ Đúng "camera-ai = home" + giữ UI + live view sẵn. Tái dùng `event_collector` async của DCNET gần nguyên (đã verified prod).
- ⚠️ Chấp nhận 2 style truy cập DB: FDW **sync** (psycopg) + collector **async** (asyncpg) — chỉ chung schema, không chung code, không xung đột.

Loại: ② Bê UI FDW sang DCNET (trái ý "home"; UI dính app → rewrite lớn). ③ Greenfield (vứt code chạy được, risk cao).

## 3. Topology đích (sau toàn bộ phase)

```
Cam Axis (ACAP edge) ─MQTT/TLS─► event_collector (async, service riêng) ─► Postgres ◄── camera-ai app (FastAPI sync, psycopg)
Cam đa hãng ─RTSP─► go2rtc ─► camera-ai (live view + YOLO optional khi cam ko có edge)         │  (UI: đếm/occupancy/group/live/fall + auth)
reid_worker (optional service, off mặc định) ─► Postgres ◄────────────────────────────────────┘
Deploy: docker-compose (Postgres + camera-ai + event_collector + cam_proxy + go2rtc + mosquitto optional)
```

**Quyết topology ingest:** giữ `event_collector` là **service riêng async** (đã verified prod, tách ingest khỏi web → web restart không mất event; ingest bền). camera-ai app **không** tự consume MQTT.

## 4. Phasing (decomposition tổng thể)

| Phase | Nội dung | Spec |
|---|---|---|
| **0. Unify DB** ⬅ spec này | Dựng Postgres trong camera-ai; port schema SQLite FDW (`events`→`incidents`, `users`, `settings`) sang Postgres; rewrite `db.py` `sqlite3`→`psycopg` sync; data-migration script | **CHI TIẾT DƯỚI** |
| 1. Module ĐẾM | Đưa `event_collector` + schema counting (`events`, `cameras`) vào Postgres chung; thêm UI đếm IN/OUT + occupancy. Parity Streamlit | spec riêng |
| 2. Module Group/Re-ID | Port page "Nhóm theo người" sang Jinja; `reid_worker` service optional (off) | spec riêng |
| 3. Modular per-customer | Feature flag mỗi cam/khách; YOLO off khi cam có ACAP | spec riêng |
| 4. Deploy/cutover | docker-compose đầy đủ; cut over khỏi Streamlit khi parity | spec riêng |

---

# PHASE 0 — Unify DB (SQLite → Postgres)

## P0.1 Mục tiêu

FDW đọc/ghi **PostgreSQL** thay vì SQLite, schema đặt nền cho việc gộp counting/Re-ID sau. Sau Phase 0: app FDW chạy y hệt hiện tại (dashboard/events/auth/settings/live/fall-detection) nhưng backend = Postgres. **Hành vi người dùng không đổi** — chỉ đổi tầng lưu trữ.

## P0.2 Schema target (Postgres)

Port 3 bảng SQLite của FDW. **Đổi tên `events`→`incidents`** (tránh đụng bảng `events` của counting DCNET sẽ vào ở Phase 1 — `events` DCNET là lượt qua vạch, khác hẳn).

```sql
CREATE TABLE incidents (              -- FDW 'events' cũ: log fall-detection + recording
    id           BIGSERIAL PRIMARY KEY,
    time         TEXT NOT NULL,         -- 'time' cũ (UTC ISO) → TEXT (KHÔNG TIMESTAMPTZ)
    time_local   TEXT,                          -- giữ chuỗi VN+7 hiển thị (như cũ)
    status       TEXT NOT NULL,
    camera       TEXT,
    confidence   REAL,
    ai_result    TEXT,
    ai_raw       TEXT,
    ai_response  TEXT,
    message      TEXT,
    error        TEXT,
    image_file   TEXT,
    teldrive_image_id TEXT, teldrive_image_name TEXT, teldrive_image_path TEXT,
    teldrive_video_id TEXT, teldrive_video_name TEXT, teldrive_video_path TEXT
);
CREATE INDEX idx_incidents_time ON incidents (time DESC);

CREATE TABLE users (                  -- không đụng DCNET (DCNET có 'employees', không 'users')
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE settings (               -- 3-tier config tier giữa; cameras/prompts = JSON trong value
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

**Ghi chú (ratify 2026-06-26):** `time`/`created_at`/`updated_at` để `TEXT` (KHÔNG `TIMESTAMPTZ`) — giữ behavior y hệt SQLite: với `dict_row`, `TIMESTAMPTZ` trả `datetime` → phá `find_matching_teldrive_image` (gọi `datetime.fromisoformat` trên chuỗi) + đổi shape JSON `/api/events`. Mandate "signatures + behavior unchanged" outrank kiểu cột. ISO string sort lexicographic vẫn đúng cho `ORDER BY time`.

**Audit va chạm tên** (vs schema DCNET `db/init.sql`): `events`=VA CHẠM→đổi `incidents`; `users`/`settings`=trống bên DCNET→OK; `cameras` (DCNET có) — FDW lưu cameras dạng JSON trong `settings`, **Phase 0 GIỮ NGUYÊN** (không gộp vào bảng `cameras` của DCNET — để Phase 3 modular xử lý). `recordings` = view/filter của `incidents` theo cột video (như hiện tại "events là bảng recordings").

## P0.3 Tầng truy cập DB — rewrite `db.py`

`sqlite3` (sync) → **`psycopg` v3 (sync)** — khớp model threaded của FDW + đồng bộ với dashboard DCNET (đã dùng psycopg). **Giữ nguyên signature** mọi hàm public (`init_db`, `get_conn`, các query, `now_iso`, `local_iso`, `invalidate_event_caches`...) → caller (`app.py`, `monitor.py`, `redis_cache.py`) không phải sửa.

Điểm phải đổi (SQLite → Postgres):
- Connection: `sqlite3.connect(path)` → `psycopg.connect(DSN)` (DSN từ env `DATABASE_URL`/`DB_*`). Pool sync (`psycopg_pool.ConnectionPool`) cho thread-safety.
- Placeholder `?` → `%s`. `row_factory` `sqlite3.Row` → `psycopg.rows.dict_row`.
- `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL`. `INSERT ... RETURNING id` thay `lastrowid`.
- `INSERT OR REPLACE`/`INSERT OR IGNORE` (settings/users) → `INSERT ... ON CONFLICT (key/username) DO UPDATE/NOTHING`.
- `executescript` → tách statement chạy lần lượt. `PRAGMA table_info` (`_ensure_column`) → `information_schema.columns` + `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
- WAL/timeout (sqlite-specific) → bỏ; Postgres lo concurrency.
- Tên bảng `events` → `incidents` trong mọi query của `db.py` (+ chỗ khác nếu có ref trực tiếp — audit `app.py`/`monitor.py`).

## P0.4 Config — `config.py`

3-tier `env/.env > settings(DB) > DEFAULT_CONFIG` giữ nguyên; chỉ đổi nguồn tier giữa từ SQLite settings → Postgres settings (qua `db.py` mới). Legacy `config.json` auto-migrate giữ (chạy 1 lần). Thêm env DSN (`DATABASE_URL` hoặc `DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME`) vào `ENV_CONFIG_KEYS`/`.env.example`.

## P0.5 Data migration (1 lần) — **OPTIONAL (greenfield, đã chốt)**

**Quyết review: greenfield — chưa có deployment FDW thật cần giữ data.** Script `migrate_sqlite_to_pg.py` (đọc `data/fall_detection.db` → copy `events`→`incidents`/`users`/`settings`, idempotent ON CONFLICT) là **optional, KHÔNG bắt buộc Phase 0**. Bỏ qua; chỉ làm sau nếu xuất hiện .db cần giữ.

## P0.6 Deploy

Thêm `docker-compose.yml` (mới trong camera-ai) tối thiểu Phase 0: service `postgres` (`pgvector/pgvector:pg16` — chuẩn bị sẵn cho Re-ID Phase 2) + service `fall_detection_web` (uvicorn). Env DSN nối 2 service. `data/` (event_images, teldrive_cache) vẫn volume. Cách chạy cũ (venv+systemd) vẫn được — DSN trỏ Postgres ngoài.

## P0.7 KHÔNG thuộc Phase 0

- `event_collector` + schema counting (`events`, `cameras` crossing) = **Phase 1**.
- UI đếm/occupancy/group = Phase 1–2.
- Gộp cameras-config vào bảng `cameras` = Phase 3.
- Async hoá FDW = KHÔNG (giữ sync threaded).

## P0.8 Verification

FDW **không có test suite** (chuẩn repo: verify bằng chạy app). Phase 0 verify:
1. `init_db` tạo 3 bảng trên Postgres sạch (psql `\dt`).
2. Chạy app → login (admin/admin tạo lần đầu vào `users` Postgres), đổi 1 setting → reload thấy persist (settings Postgres).
3. Trigger 1 incident (hoặc insert tay) → hiện ở dashboard/events (đọc `incidents`).
4. Data-migration: nếu có `.db` cũ → chạy script → số row `incidents/users/settings` khớp.
5. Smoke script psycopg: CRUD `incidents` + `ON CONFLICT` settings/users.

## P0.9 Rủi ro & giảm thiểu

| Rủi ro | Giảm thiểu |
|---|---|
| `db.py` rewrite chạm nhiều caller | Giữ nguyên signature hàm public → caller không đổi; audit ref `events` trực tiếp ngoài db.py |
| SQLite-ism rải rác (`?`, OR REPLACE, lastrowid) | Grep toàn FDW `sqlite3`/`?`/`executescript`/`lastrowid` trước khi sửa; checklist trong plan |
| Mất data deployment cũ | Script migrate idempotent + verify đếm row |
| Thread-safety Postgres | `psycopg_pool.ConnectionPool` thay 1 connection/thread |
| Va chạm tên `events` | Đổi `incidents` ngay Phase 0 (trước khi counting `events` vào Phase 1) |

## P0.10 Quyết định đã chốt (review 2026-06-26)

1. **DB target = Postgres MỚI trong camera-ai** (compose riêng, `pgvector/pgvector:pg16`). Độc lập prod DCNET live, gộp schema counting/Re-ID vào dần ở phase sau.
2. **Greenfield** — chưa có data FDW thật → data-migration script (P0.5) **optional, skip**. Verification Phase 0 bỏ bước migrate-data; tập trung init schema + CRUD trên Postgres sạch.


---
## Spec index (5 phase)
| Phase | Spec | Plan | Trạng thái |
|---|---|---|---|
| 0 Unify DB | (trong doc này) | [plan](../plans/2026-06-26-phase0-unify-db-postgres.md) | ✅ DONE + merged (PR #1) |
| 1 Đếm | [phase1](2026-06-26-phase1-counting-design.md) | chưa | spec ✅ |
| 2 Group/Re-ID | [phase2](2026-06-26-phase2-group-reid-design.md) | chưa | spec ✅ |
| 3 Modular per-customer | [phase3](2026-06-26-phase3-modular-percustomer-design.md) | chưa | spec ✅ |
| 4 Deploy/cutover | [phase4](2026-06-26-phase4-deploy-cutover-design.md) | chưa | spec ✅ |

Implement tuần tự: mỗi phase spec → writing-plans → subagent execute → PR → merge.
