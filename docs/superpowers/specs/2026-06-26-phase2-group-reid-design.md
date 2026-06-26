# Design — Phase 2: Module Group / Re-ID

**Ngày:** 2026-06-26
**Trạng thái:** DESIGN (chờ review)
**Phase:** 2 / 5 (xem tổng thể: `2026-06-26-dcnet-platform-migration-design.md`)
**Tiền đề:** Phase 1 DONE + merged — Postgres chứa `cameras` + `events`, `event_collector` chạy, trang "Đếm ra/vào" live.
**Nguồn port:** repo `dcnet-cloud/camera` — `services/reid_worker/` (pipeline Re-ID async), `services/dashboard/src/dashboard/pages/2_Nhom_Theo_Nguoi.py` + `groups.py` (UI), schema Re-ID trong `db/init.sql` (`person_group`/`appearance`/`appearance_crop`), findings `docs/reid-capture-findings.md`.

---

## 1. Mục tiêu

Port khả năng **Group / Re-ID (gom lượt theo người)** từ DCNET camera vào camera-ai như **module tùy chọn, TẮT mặc định**:

1. **Schema Re-ID** (`person_group`, `appearance`, `appearance_crop`, vector 512-d) vào Postgres chung (đã pgvector/pg16 từ Phase 0).
2. **`reid_worker`** — service async riêng (aiomqtt + asyncpg + OSNet body embed + cosine match pgvector), port gần nguyên DCNET; không start trừ khi bật tường minh.
3. **Trang "Nhóm theo người"** — port Streamlit page sang Jinja/FDW: group card + crop ảnh + visit count + badge tái xuất.
4. **Crop quality filter** `class.score ≥ 0.5` (đã verified DCNET 2026-06-25: diệt 83% crop rác) giữ nguyên.

**Hành vi mặc định khi deploy:** `reid_worker` không start, nav link "Nhóm theo người" ẩn (hoặc trang hiển thị banner "module chưa bật"), Postgres chứa schema nhưng trống. Bật lại = bước chủ động (xem §2 "Đường bật lại").

**Không thuộc Phase 2:** toggle per-customer (Phase 3); cutover prod (Phase 4); face recognition thật / attendance (branch `main-backup-2306`, deferred dài hạn); LLM query trên data group (deferred, research doc `camera/docs/research/`).

---

## 2. Quyết định đã chốt (review 2026-06-26)

### 2.1 Module TẮT mặc định — build-and-shelve

**Lý do kép — cả hai phải giải trước khi bật:**

| # | Blocker | Chi tiết | Giải khi nào |
|---|---|---|---|
| 1 | **Camera placement** | Cam dome trần M3216-LVE: face NO-GO (0 crop frontal), body over-merge (đồng phục DCNET + top-down → gộp cả nam+nữ tại threshold 0.6). 3 tín hiệu độc lập hội tụ về vị trí cam, không phải lỗi code. | Đặt lại cam thấp/nghiêng cửa (full-body + mặt frontal). |
| 2 | **License non-commercial** | OSNet (`torchreid`) = non-commercial; InsightFace (`buffalo_s`) = non-commercial + AGPL. Camera-ai vision = bán module theo khách → không hợp lệ khi enable cho khách trả tiền. Guard `REID_COMMERCIAL_MODE=true → sys.exit(1)` đã có trong `reid_worker/main.py:186-189`. | Swap stack → permissive (xem §9 Open questions). |

Code + filter giữ lại, verified, sẵn sàng bật lại. Spec này = design để implement, **không activate**.

### 2.2 Schema: chỉ 3 bảng Re-ID group, không port bảng recognition cũ

Port **chỉ** `person_group` / `appearance` / `appearance_crop` từ `db/init.sql`. **Không** port `employees`, `embeddings`, `face_pool`, `recognitions`, `attendance_sessions` — thuộc stack recognition/attendance (branch `main-backup-2306`, scope deferred riêng biệt).

FK: `person_group.cam_id → cameras(id)` — Phase 2 phụ thuộc Phase 1's `cameras` table.

### 2.3 `reid_worker` = service async riêng, kiến trúc song song event_collector

Tái dùng kiến trúc Phase 1: service tách riêng (aiomqtt + asyncpg), web FDW sync psycopg chỉ ĐỌC. Worker ghi `person_group`/`appearance`/`appearance_crop` + crop files; FDW đọc Postgres + serve crop qua route FileResponse. Không chung code, chỉ chung schema + filesystem volume.

### 2.4 Crop image serving = route `/api/reid-crop/{group_id}/{filename}` + FileResponse — **cùng volume**

Theo pattern FDW hiện có (`/api/event-image/{filename}` → `FileResponse`, `fall_detection_web/app.py:159`): thêm route `/api/reid-crop/{group_id}/{filename}` với path validation (chặn path traversal), auth JWT như route event-image.

**Vấn đề volume:** FDW hiện dùng `DATA_DIR = Path(__file__).resolve().parent / "data"` = `/app/data` (mounted `fdw_data:/app/data`); worker DCNET default `DATA_DIR=/data` — **2 path khác nhau, 2 named volume khác nhau**. Nếu giữ nguyên, worker ghi vào một filesystem, FDW đọc filesystem khác → `/api/reid-crop/` 404 toàn bộ.

**Giải pháp (cần enforce trong compose):** worker và FDW mount **cùng 1 named volume** tại **cùng 1 path**. Đề xuất đơn giản nhất: worker `DATA_DIR=/app/data` (đổi từ default `/data`) + mount `fdw_data:/app/data` (volume FDW đã có). FDW thêm `REID_CROPS_DIR = DATA_DIR / "reid_crops"` (cùng DATA_DIR). Cả hai trỏ cùng `fdw_data:/app/data/reid_crops/`.

`REID_CROPS_DIR` trong FDW (`fall_detection_web/db.py` hoặc app.py) = `DATA_DIR / "reid_crops"`. Route validate: `Path(group_id)` phải integer, `filename` chỉ `.jpg` không có `/` → trả `REID_CROPS_DIR / str(group_id) / filename`. Không dùng StaticFiles mount (FDW không có pattern này).

### 2.5 FACE default TẮT, MQTT client-id riêng

- `REID_FACE_ENABLED` default **`false`** (port từ DCNET findings: 0 crop face dùng được ở dome). Code DCNET default `true` (`main.py:48`) — **đổi khi port**.
- MQTT client-id default `reid_worker_cameraai` (DUY NHẤT, không đá `reid_worker` prod DCNET nếu có). Topic `poc/objsnap` (cam pubisher `pocsnap`). Broker cloud TLS.

### 2.6 "Đường bật lại" — cụ thể hóa để "shelved" là trung thực

Bật Re-ID sau khi Phase 2 implement đòi 4 bước liên tiếp:
1. **Cấu hình lại cam:** recreate publisher `pocsnap` trên cam Axis (`config/rest/analytics-mqtt/v1beta/publishers/pocsnap`) — đã DELETE sau test 2026-06-25 (`docs/reid-capture-findings.md:117`).
2. **Đặt lại cam:** mount thấp/nghiêng hướng cửa, full-body frontal.
3. **License swap** (nếu commercial): thay OSNet → stack permissive (xem §9).
4. **Start service:** `REID_ENABLED=true` → compose thêm service `reid_worker`; bật nav link "Nhóm theo người".

---

## 3. Kiến trúc & dataflow

```
Cam Axis (object_snapshot ACAP) ─poc/objsnap─► cloud broker camera-test.dcnet.vn:8883 TLS
                                                          │ (chỉ khi cam publisher pocsnap tồn tại)
                                                          ▼
                                              reid_worker (service async, OFF mặc định)
                                               ├─ parse_objsnap → ObjSnap dict
                                               ├─ Assembler: gom theo track_id (timeout 3s)
                                               ├─ embed_appearance: filter score≥0.5 + px≥96×192
                                               │    → OSNet body 512-d fuse multi-frame L2-norm
                                               │    → InsightFace face 512-d (FACE_ENABLED=false)
                                               ├─ decide_match: cosine vs live groups (TTL 2h)
                                               │    → create_group hoặc add_appearance_to_group
                                               └─► Postgres (asyncpg)
                                                    person_group / appearance / appearance_crop
                                                    + /data/reid_crops/<group_id>/*.jpg (volume)
                                                          │
                  ┌───────────────────────────────────────┘
                  ▼ sync psycopg (read-only từ FDW)
         FDW app — /groups route (JWT auth)
          ├─ query person_group WHERE last_seen >= now()-2h ORDER BY last_seen DESC
          ├─ render groups.html: group cards (badge + visit_count + timestamps)
          ├─ expandable: appearance_crop list per group
          └─ crop images: GET /api/reid-crop/{group_id}/{filename} → FileResponse
```

**Topology note:** `reid_worker` và FDW đều mount cùng volume `/data/reid_crops/` (worker rw, FDW ro). Crops write async, đọc sync — không cần lock (worker write-once per file, FDW chỉ đọc). Nếu crop chưa tồn tại khi FDW query → route trả 404 → template ẩn `<img>` (graceful).

**purge_loop:** worker chạy mỗi 5 phút, xóa `person_group` hết TTL 2h (CASCADE xóa `appearance` + `appearance_crop` liên quan). Crop files trên filesystem **không tự xóa** (purge_loop của DCNET chỉ DELETE DB row) — Open question §9.

---

## 4. Schema (thêm vào Postgres Phase 1)

Port nguyên 3 bảng từ `dcnet-cloud/camera/db/init.sql:127-163`. `CREATE EXTENSION IF NOT EXISTS vector` đã có từ Phase 0.

```sql
-- Phase 2: Re-ID group schema (module optional, OFF mặc định)
-- FK cam_id → cameras(id) yêu cầu Phase 1 đã tạo bảng cameras.

CREATE TABLE IF NOT EXISTS person_group (
    id               BIGSERIAL PRIMARY KEY,
    cam_id           INT REFERENCES cameras(id),
    first_seen       TIMESTAMPTZ NOT NULL,
    last_seen        TIMESTAMPTZ NOT NULL,
    visit_count      INT NOT NULL DEFAULT 1,
    rep_body_vector  vector(512) NOT NULL,
    rep_face_vector  vector(512),          -- NULL khi FACE_ENABLED=false
    rep_crop_path    TEXT,                 -- path filesystem crop đại diện
    created_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS person_group_last_seen ON person_group (last_seen DESC);
CREATE INDEX IF NOT EXISTS person_group_body_ivf ON person_group
    USING ivfflat (rep_body_vector vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS appearance (
    id           BIGSERIAL PRIMARY KEY,
    group_id     BIGINT REFERENCES person_group(id) ON DELETE CASCADE,
    cam_id       INT REFERENCES cameras(id),
    ts           TIMESTAMPTZ NOT NULL,
    body_vector  vector(512) NOT NULL,
    face_vector  vector(512),
    track_id     TEXT,
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS appearance_group ON appearance (group_id, ts DESC);

CREATE TABLE IF NOT EXISTS appearance_crop (
    id             BIGSERIAL PRIMARY KEY,
    appearance_id  BIGINT REFERENCES appearance(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('body','face')),
    path           TEXT NOT NULL,          -- absolute path trong container
    frame_idx      INT,
    quality        REAL,                   -- class.score từ object_snapshot
    created_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS appearance_crop_app ON appearance_crop (appearance_id);
```

**Ghi chú schema:**
- `IF NOT EXISTS` trên mọi object → init idempotent, chạy lại an toàn.
- `person_group.rep_crop_path`: path filesystem tuyệt đối trong container (`/data/reid_crops/<group_id>/<app_id>_body_<idx>.jpg`). FDW dùng để serve ảnh đại diện.
- ivfflat index `lists=100` giữ nguyên DCNET — đủ cho TTL window nhỏ (số group live << 100 trong cửa sổ 2h); rebuild không cần.
- **Không thêm** `employees`, `embeddings`, `face_pool`, `recognitions`, `attendance_sessions` — bảng recognition/attendance cũ, scope riêng.

---

## 5. Components & files

### a) `services/reid_worker/` (MỚI — port từ DCNET gần nguyên)

Cấu trúc y hệt DCNET:
```
services/reid_worker/
    Dockerfile
    requirements.txt
    src/reid_worker/
        __init__.py
        assembler.py    # Assembler: gom track_id theo timeout wall-clock
        embed.py        # BodyEmbedder (OSNet), FaceEmbedder (InsightFace), body_crop_ok, fuse_embeddings
        matcher.py      # cosine(), decide_match()
        parser.py       # parse_objsnap(): Axis payload → ObjSnap dict
        repo.py         # ReidRepo (asyncpg): create_group, add_appearance, insert_crop, purge_expired
        main.py         # amain(): consume_loop + flush_loop + purge_loop; REID_COMMERCIAL_MODE guard
    tests/
        test_embed.py   # body_crop_ok, fuse_embeddings, l2norm
        test_matcher.py # cosine, decide_match
        test_parser.py  # parse_objsnap (topic/payload → dict)
        test_assembler.py # flush_expired, body/face split
```

**Thay đổi khi port (so với DCNET nguồn):**

| File | Thay đổi |
|---|---|
| `main.py` | `REID_FACE_ENABLED` default `"false"` (DCNET default `"true"` — findings mandate false). `MQTT_CLIENT_ID` default `"reid_worker_cameraai"`. DSN từ env chung Postgres camera-ai. |
| `requirements.txt` | Port nguyên (aiomqtt, asyncpg, structlog, numpy, opencv-headless, onnxruntime, insightface, torch, torchvision, torchreid, gdown). Model weights download lần đầu boot. |
| `Dockerfile` | Port/adapt từ DCNET Dockerfile (Python 3.12, PYTHONPATH=/app/src, image heavy ~3–4GB vì torch). |

**Không port:** `raw_capture.py` (diagnostic tool, không thuộc pipeline; bỏ lại DCNET).

### b) Schema migration (thêm vào init hoặc script riêng)

File `db/reid_schema.sql` (hoặc append vào `db/init.sql` Phase 1 sau section cameras/events). Chạy `CREATE TABLE IF NOT EXISTS` → idempotent. Gọi khi `init_db()` hoặc migrate script riêng.

**Tùy chọn gọi:** Phase 2 có thể append SQL vào `fall_detection_web/db.py:init_db()` sau section Phase 1, hoặc thêm file `db/phase2_reid_schema.sql` chạy lúc compose init. Cụ thể hóa khi viết plan — chốt theo pattern Phase 1.

### c) `fall_detection_web/db.py` (thêm hàm đọc groups, sync psycopg)

Thêm 3 hàm (giữ pattern pool psycopg dict_row hiện có, KHÔNG đổi hàm cũ):
- `reid_live_groups(ttl_hours=2, cam_id=None) -> list[dict]`: SELECT `person_group` WHERE `last_seen >= now() - interval`, ORDER BY `last_seen DESC`.
- `reid_group_crops(group_id: int, limit=40) -> list[dict]`: JOIN `appearance` + `appearance_crop` WHERE `group_id = ?`, ORDER BY `ts DESC`, LIMIT.
- `reid_stats(ttl_hours=2) -> dict`: `{unique_count, reentry_count}` — COUNT groups + COUNT WHERE `visit_count > 1`.

### d) `fall_detection_web/app.py` — route groups + crop image serving

**Thêm:**
- `GET /groups` (JWT auth, Jinja template `groups.html`): query `reid_live_groups()` + `reid_stats()`, render. Auto-refresh nhẹ (polling interval dài hơn Phase 1: 10–15s — data Re-ID thay đổi chậm hơn crossline).
- `GET /api/groups` (JWT auth): JSON `{groups, stats}` cho polling JS.
- `GET /api/reid-crop/{group_id}/{filename}` (JWT auth): path validation (reject `..`, non-`.jpg`, không tồn tại) → `FileResponse(REID_CROPS_DIR / group_id / filename, media_type="image/jpeg")`. Pattern y hệt `/api/event-image/{filename}` (`app.py:159-189`): ETag + Cache-Control immutable.

**REID_CROPS_DIR** = env `DATA_DIR` / `"reid_crops"` (khớp worker `CROP_DIR`). Đọc từ `db.py` hoặc config.

### e) `fall_detection_web/templates/groups.html` (MỚI)

Jinja template, extend base layout FDW (với JWT + nav). Các phần:
- Banner nếu module chưa bật (detect bằng `reid_enabled` từ config/env): "Module Re-ID chưa bật — xem hướng dẫn bật lại".
- 2 số trên: "Khách duy nhất (Xh)" + "Số lượt tái xuất".
- Grid group card (4 cột, như Streamlit): ảnh đại diện (route `/api/reid-crop/...`), badge (🔁 / 🆕), visit_count, first_seen/last_seen (VN+7 `%H:%M:%S %d/%m`).
- Accordion/details per card: danh sách crop của group (ảnh nhỏ + kind + score + timestamp).
- Auto-refresh: `<meta http-equiv="refresh">` hoặc fetch JS polling `/api/groups` mỗi 15s.

### f) `docker-compose.yml` — service `reid_worker` (profile optional)

```yaml
reid_worker:
  build: ./services/reid_worker
  profiles: ["reid"]          # opt-in: không start mặc định
  env_file: .env
  environment:
    MQTT_CLIENT_ID: reid_worker_cameraai
    REID_FACE_ENABLED: "false"
    REID_TOPIC: poc/objsnap
    DATA_DIR: /app/data       # QUAN TRỌNG: phải khớp FDW DATA_DIR (/app/data)
  volumes:
    - fdw_data:/app/data      # CÙNG named volume + CÙNG path với fall_detection_web service
  depends_on:
    postgres:
      condition: service_healthy
  restart: unless-stopped
```

**⚠️ Volume constraint:** FDW service mount `fdw_data:/app/data`. Worker PHẢI mount `fdw_data:/app/data` (không phải `data:/data` hoặc path khác). Nếu sai → crop route 404 toàn bộ. Xác nhận trong compose review trước khi merge.

Bật bằng: `docker compose --profile reid up -d reid_worker`. Không ảnh hưởng các service khác khi off.

### g) Nav link "Nhóm theo người"

Ẩn mặc định (hoặc render với banner disabled) khi `REID_ENABLED=false`. Bật nav khi `REID_ENABLED=true` (env). Chi tiết toggle mechanics = Phase 3 — Phase 2 chỉ cần đơn giản: route `/groups` vẫn exist nhưng hiển thị banner khi chưa bật.

---

## 6. Error handling

- **Worker không start (mặc định):** Postgres schema trống → FDW query trả `[]` → trang hiển thị banner "chưa có nhóm nào / module chưa bật" — không lỗi.
- **Crop file chưa có** (race: DB row ghi nhưng file chưa flush, hoặc volume unmount): route `/api/reid-crop/` trả 404 → template render placeholder "(chưa có ảnh)" — graceful.
- **Worker reconnect MQTT:** loop retry 2s (`consume_loop`) — port nguyên DCNET pattern.
- **DB transaction / constraint:** `person_group` không có UNIQUE constraint crossline như `events` (Re-ID không idempotent theo design — duplicate appearance → group count tăng). Worker crash + restart có thể tạo duplicate appearance nếu appearance đang flush lúc crash. Acceptable cho POC; note trong doc.
- **Cam publisher pocsnap absent:** worker subscribe `poc/objsnap` → không nhận message → không lỗi, groups trống. Log level INFO "mqtt_connected" nhưng 0 message. Không cần alert.
- **`REID_COMMERCIAL_MODE=true` guard:** worker exit(1) ngay boot với log `commercial_mode_blocked` — cố ý, không phải bug.
- **Crop filesystem không purge:** DB DELETE CASCADE xóa row, file `.jpg` trên disk orphan. Cần cron hoặc script dọn dẹp — ghi nhận Open question (§9); acceptable scope Phase 2 vì TTL window ngắn (2h) + POC data nhỏ.

---

## 7. Testing

**Unit tests (pure, không I/O) — port từ DCNET, chạy `pytest`:**

| Module | Test | Ghi chú |
|---|---|---|
| `embed.py` | `test_embed.py`: `body_crop_ok(score,w,h)` — các case score=0/0.5/0.6, px biên; `fuse_embeddings` L2-norm; `l2norm` zero-vec | Tests verified pass DCNET 2026-06-25 |
| `matcher.py` | `test_matcher.py`: `cosine` ortho/parallel/zero; `decide_match` no-match/match/threshold-edge | Pure numpy |
| `parser.py` | `test_parser.py`: `parse_objsnap` payload đầy đủ, thiếu `data`, thiếu `track`, b64 bad, `class.score` None | Pure |
| `assembler.py` | `test_assembler.py`: `flush_expired` timeout, body/face split, multi-add | Pure |

**Không unit-test** `BodyEmbedder` / `FaceEmbedder` (model-dependent, load ~300MB+ torch — CI không thích hợp). `ReidRepo` (asyncpg live) — verify bằng smoke.

**Smoke verify (thủ công, giống Phase 0/1 style):**
1. `init_db` tạo 3 bảng Re-ID trên Postgres: `psql \dt` thấy `person_group`/`appearance`/`appearance_crop`.
2. Insert tay 1 `person_group` row (mock vector `'[0.0,...]'::vector`), 1 `appearance`, 1 `appearance_crop` → FDW `/groups` hiển thị 1 card (no worker needed).
3. Route `/api/reid-crop/{group_id}/{filename}` với file `.jpg` giả → 200 FileResponse.
4. Route với path `../etc/passwd` → 404 (path validation).
5. Live end-to-end (khi bật module, cam publisher `pocsnap` active): đi qua cửa 3 lần → psql COUNT `person_group` ≥ 1, `appearance` ≥ 3, `/groups` hiện card.

**FDW không có pytest web layer** (xác nhận Phase 1) — verify bằng browser: login → `/groups` render → card hiện ảnh → accordion crop expand.

---

## 8. Decompose (cho writing-plans)

Thứ tự implement đề xuất:

1. **Schema Re-ID** — append 3 bảng vào init (hoặc `db/reid_schema.sql`); chạy `CREATE IF NOT EXISTS` trên Postgres dev; psql verify.
2. **`services/reid_worker/` port** — copy 6 file src từ DCNET, đổi 2 default env (`FACE_ENABLED=false`, `CLIENT_ID`), viết Dockerfile, port 4 test file, `pytest` pass, build image.
3. **`docker-compose.yml` service `reid_worker`** — profile `reid`, env, volume, depends_on postgres healthy; smoke start với `--profile reid`.
4. **`db.py` hàm đọc groups** — `reid_live_groups`, `reid_group_crops`, `reid_stats`; smoke tay (insert mock data, gọi hàm, verify list).
5. **Route `/groups` + `/api/groups` + `/api/reid-crop/`** — app.py + groups.html; verify render với mock data + crop serving + path validation.
6. **Banner disabled-state + nav link ẩn** — env `REID_ENABLED` control; verify mặc định = banner, không có nav link.

---

## 9. Open questions (confirm khi review)

| # | Câu hỏi | Đề xuất hiện tại | Ưu tiên |
|---|---|---|---|
| OQ1 | **License swap roadmap:** OSNet (non-commercial) → permissive alternative nào? Các ứng cử viên như `fast-reid`, `clip-reid`, v.v. — **license cụ thể cần verify trước khi chọn** (không khẳng định ở đây). Research `dcnet-cloud/camera/docs/research/2026-06-23-reid-recognition-stack-research.md` là điểm xuất phát. Timing: Phase 2 port as-is với guard; swap trước khi activate cho khách. | Port stack hiện tại, không bật cho commercial, note trong Dockerfile comment. | High khi activate |
| OQ2 | **Crop file purge:** worker `purge_expired` xóa DB row (CASCADE) nhưng không xóa file `/data/reid_crops/*.jpg` → accumulate vô hạn. Cần cron script `find /data/reid_crops -name "*.jpg" -mmin +120 -delete` hoặc worker purge_loop mở rộng. | Thêm purge file trong `purge_loop` Phase 2 (rẻ, aligned với DB purge). | Medium |
| OQ3 | **`rep_body_vector` stale (drift):** findings ghi `report minor` — representative vector KHÔNG update khi re-entry → drift → same person tạo group mới. Cân nhắc running mean khi `add_appearance_to_group`. | Giữ nguyên DCNET (không fix Phase 2); note là known limitation; fix Phase 3 nếu bật. | Low (shelved) |
| OQ4 | **Cam publisher `pocsnap` recreate:** sau khi DELETE 2026-06-25, cần VAPIX call để recreate khi bật lại. Đưa vào ops runbook hay script? | Ops note trong CLAUDE.md + Phase 4 deploy doc. | Low (shelved) |
| OQ5 | **UI auto-refresh interval:** `/groups` data thay đổi chậm (Re-ID event << crossline). 15s đề xuất — confirm hay dùng manual refresh? | 15s polling JS `/api/groups` (nhẹ hơn Phase 1 3s). | Low |
| OQ6 | **`person_group` duplicate appearance on crash:** worker restart → flush mid-appearance → duplicate row (không có UNIQUE constraint). Acceptable POC? Hay thêm `UNIQUE (group_id, cam_id, ts, track_id)`? | Acceptable POC; note. | Low |

---

## 10. Phụ lục — Cross-reference nguồn

| Artifact | Mô tả |
|---|---|
| `dcnet-cloud/camera/services/reid_worker/src/reid_worker/main.py` | amain, consume/flush/purge loop, COMMERCIAL guard, env defaults |
| `dcnet-cloud/camera/services/reid_worker/src/reid_worker/embed.py` | BodyEmbedder (OSNet), body_crop_ok, fuse_embeddings |
| `dcnet-cloud/camera/services/reid_worker/src/reid_worker/matcher.py` | cosine, decide_match |
| `dcnet-cloud/camera/services/reid_worker/src/reid_worker/assembler.py` | Assembler track-timeout |
| `dcnet-cloud/camera/services/reid_worker/src/reid_worker/parser.py` | parse_objsnap, class.score extraction |
| `dcnet-cloud/camera/services/reid_worker/src/reid_worker/repo.py` | ReidRepo asyncpg: create_group, add_appearance, insert_crop, purge_expired, _vec_literal |
| `dcnet-cloud/camera/services/reid_worker/requirements.txt` | deps: aiomqtt, asyncpg, torchreid, insightface, onnxruntime |
| `dcnet-cloud/camera/db/init.sql:127-163` | Schema person_group / appearance / appearance_crop (source of truth) |
| `dcnet-cloud/camera/services/dashboard/src/dashboard/pages/2_Nhom_Theo_Nguoi.py` | Streamlit group page (UI logic to port) |
| `dcnet-cloud/camera/services/dashboard/src/dashboard/groups.py` | fmt_group_card helper (pure) |
| `dcnet-cloud/camera/docs/reid-capture-findings.md` | GO/NO-GO findings: face NO-GO, body over-merge, filter verified, enable-path |
| `camera-ai/fall_detection_web/app.py:159-189` | `/api/event-image` FileResponse pattern (template cho crop route) |

---
## Liên quan
- Tổng thể: [migration design](2026-06-26-dcnet-platform-migration-design.md)
- Trước: [Phase 1 Đếm](2026-06-26-phase1-counting-design.md) · Sau: [Phase 3 Modular](2026-06-26-phase3-modular-percustomer-design.md)
