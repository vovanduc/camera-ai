# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

This is a **monorepo of two independent applications** that share only a domain (AI camera analysis) — they have **no shared code** and are deployed separately. Treat each as its own project with its own constraints.

| Directory | What it is | Deploy target |
| --- | --- | --- |
| [`simple_ai_vision/`](simple_ai_vision) | A deliberately minimal Home Assistant Add-on: analyzes JPEG snapshots via an OpenAI-compatible Vision API and sends Telegram alerts. | HA Supervisor add-on (Docker, amd64/aarch64) |
| [`fall_detection_web/`](fall_detection_web) | A standalone self-hosted web app: local YOLOv8 person detection → AI Vision verification → Telegram alert → incident recording/timeline. | VPS / mini-PC via uvicorn + systemd |
| [`docs/diagrams/`](docs/diagrams) | Generated architecture & flow diagrams — **8 diagrams** covering both `fall_detection_web` and `simple_ai_vision`, including **3 dedicated to the AI flow** (verification sequence, data pipeline, fault tolerance). Gallery `index.html`, index [`README.md`](docs/diagrams/README.md), sources `src/*.json`. | Static HTML, not deployed |

`repository.yaml` defines the HA add-on repository; only `simple_ai_vision/` is published through it. All in-repo docs (README, AGENTS.md) and commit messages are written in **Vietnamese**; default to UTF-8 and match that for user-facing strings.

**Diagrams** ([`docs/diagrams/`](docs/diagrams)) are produced by the `dcnet-diagram` skill: each `.html` is self-contained (dark/light toggle, PNG/SVG export) and re-rendered from its JSON-IR in `docs/diagrams/src/`. Start from [`docs/diagrams/README.md`](docs/diagrams/README.md) (file index + how-to) and [`docs/diagrams/EXPLAINER.md`](docs/diagrams/EXPLAINER.md) (per-diagram detail + provenance). To update a diagram, edit the `src/*.json`, re-run the matching renderer, then rebuild the gallery (`build_gallery.py manifest.json .`). Keep diagrams in sync when either app's flow changes. This `docs/diagrams/` folder is mirrored identically in the sibling `camera-check` repo.

## DCNET Platform Migration (2026-06-26)

camera-ai đang trở thành **sản phẩm hợp nhất** của DCNET: đổ logic từ repo `dcnet-cloud/camera` (đếm người ra/vào + Re-ID) vào `fall_detection_web`, mỗi feature = module bật/tắt per-customer, DB thống nhất PostgreSQL. Chia 5 phase, mỗi phase 1 spec→plan→PR. **Design specs ở `docs/superpowers/specs/`, plans ở `docs/superpowers/plans/`.**

> **🟢 STATUS (2026-06-26): MIGRATION CODE-COMPLETE — CHƯA DEPLOY PROD.** Phase 0-3 implemented+merged (PR #1-4), Phase 4 = **PREP only** merged (PR #5: artifacts + runbook, KHÔNG cutover). Còn lại 1 việc: **deploy VM thật** (camera-test.dcnet.vn) — session riêng qua skill `dcnet-deploy` theo `docs/ops/2026-06-26-phase4-cutover-runbook.md` (cần cred + cutover window). Prod hiện vẫn chạy stack Streamlit DCNET cũ — camera-ai chưa lên prod. Dev test: `docker compose up -d` → `http://localhost:8090` (login `admin/admin`, đổi ngay khi prod). **UI (PR #6):** mọi trang dùng chung 1 sidebar trái (`templates/_sidebar.{html,css,js}` — single source: markup + style + icon hydration) + topbar header; sửa nav 1 chỗ áp hết. `index.html` = reference. **Registry camera ĐÃ HỢP NHẤT (PR #7):** chỉ còn **1 trang `/cameras`** quản mọi camera trên bảng Postgres `cameras` (nguồn duy nhất); mỗi camera bật/tắt 4 module qua pill (Đếm / Ngã / Re-ID / Live). `/modules` → redirect `/cameras`. settings-JSON `cameras` bỏ vai trò (migrate 1 lần lúc boot, match theo tên).

| Phase | Spec | Trạng thái |
|---|---|---|
| Tổng thể | [migration design](docs/superpowers/specs/2026-06-26-dcnet-platform-migration-design.md) | — |
| 0. Unify DB (SQLite→Postgres) | (trong migration design) + [plan](docs/superpowers/plans/2026-06-26-phase0-unify-db-postgres.md) | ✅ DONE + merged (PR #1) |
| 1. Module Đếm | [phase1-counting](docs/superpowers/specs/2026-06-26-phase1-counting-design.md) + [plan](docs/superpowers/plans/2026-06-26-phase1-counting.md) | ✅ DONE (pipeline live-proven — organic crossings thật 28 IN/30 OUT) |
| 2. Module Group/Re-ID | [phase2-group-reid](docs/superpowers/specs/2026-06-26-phase2-group-reid-design.md) + [plan](docs/superpowers/plans/2026-06-26-phase2-group-reid.md) | ✅ DONE (shelved, OFF mặc định; image build defer x86) |
| 3. Modular per-customer | [phase3-modular-percustomer](docs/superpowers/specs/2026-06-26-phase3-modular-percustomer-design.md) + [plan](docs/superpowers/plans/2026-06-26-phase3-modular-percustomer.md) | ✅ DONE + registry hợp nhất (PR #7) |
| 4. Deploy/cutover | [phase4-deploy-cutover](docs/superpowers/specs/2026-06-26-phase4-deploy-cutover-design.md) + [plan](docs/superpowers/plans/2026-06-26-phase4-deploy-cutover.md) | 🟡 PREP DONE (artifacts ready; VM cutover pending) |

**Phase 0 đã đổi:** `db.py` SQLite→Postgres (psycopg), bảng FDW `events`→`incidents`. Implement tuần tự (mỗi phase phụ thuộc phase trước). Khi implement 1 phase: load spec đó → `writing-plans` → `subagent-driven-development` → PR → merge.

**Phase 1 đã thêm:** service `services/event_collector/` (async aiomqtt+asyncpg, store-only MQTT→Postgres, idempotent INSERT, `MQTT_CLIENT_ID=event_collector_cameraai` DUY NHẤT — đọc ké broker cloud `camera-test.dcnet.vn:8883` TLS, KHÔNG kick collector DCNET prod); bảng `cameras`+`events` trong `init_db()` (collector cũng tự `ensure_schema` boot, tránh race); `counting.py` (pure VN+7 bucketing); route `/counting`+`/api/counting` (poll 3s) + `templates/counting.html`. Đếm = COUNT rows query-time, occupancy clamp ≥0. ⚠️ **Phase 4 reconcile:** FDW có 2 file requirements diverge (`requirements.txt` `+cpu` wheels vs `requirements.docker.txt` plain torch — workaround build arm64); chốt 1 strategy cho x86 prod.

**Phase 2 đã thêm (SHELVED, OFF mặc định):** service `services/reid_worker/` (async aiomqtt+asyncpg+OSNet/InsightFace, **profile `reid`** — `docker compose --profile reid up` mới start; `MQTT_CLIENT_ID=reid_worker_cameraai`, topic `poc/objsnap`, FACE off, OQ2 file-purge trong purge_loop); 3 bảng pgvector `person_group`/`appearance`/`appearance_crop` + ivfflat trong `init_db()`; `db.py` read fns (`reid_live_groups`/`reid_group_crops`/`reid_stats` + `REID_CROPS_DIR`); routes `/groups`+`/api/groups`+`/api/reid-crop/{group_id}/{filename}` (FileResponse, path-validated) + `templates/groups.html` (banner khi OFF, poll 15s) + nav link. Worker + FDW share volume `fdw_data:/app/data` (CRITICAL — crop serving). **2 blocker chưa giải (build-and-shelve):** cam dome placement NO-GO + OSNet/InsightFace non-commercial (`REID_COMMERCIAL_MODE=true`→exit). ⚠️ **Image build DEFERRED x86** (arm64 torch blocked) — Phase 2 verify = schema+pure tests(24, numpy-only)+queries+UI mock; build+live worker khi activate. Đường bật lại: spec §2.6.

**Phase 3 đã thêm (LIGHT scope — disjoint camera sets + greenfield):** 4 cột flag `counting/fall_detection/reid/live_enabled` trên bảng `cameras` (`enabled` = master switch; module chạy ⟺ enabled AND flag) + 2 partial index + seed Axis (counting+live); db helpers `list_cameras_for_module`/`list_cameras_all`/`update_camera_modules`; counting page filter `counting_enabled` (JOIN cameras, regression-safe); trang `/modules` + `/api/camera-modules` GET/POST (SPA toggle UI, bảng cameras). Spec gốc viết blind — đã thêm section "Thực tế (audit)" đầu spec. ✅ **Registry ĐÃ HỢP NHẤT (PR #7):** settings-JSON ↔ cameras-table merge ĐÃ giải — bảng `cameras` là nguồn duy nhất, `config.read_config()` đọc cameras từ bảng, `write_config()` không ghi key `cameras` (chống split-brain), `migrate_cameras_to_table()` migrate 1 lần lúc boot (idempotent, match theo tên), `monitor.py` chạy YOLO ⟺ `enabled AND fall_detection_enabled`. 1 trang `/cameras` quản mọi cam; `/modules` → redirect. reid_worker flag-gate vẫn shelved. ✅ **Pipeline đếm live-proven:** 28 IN/30 OUT organic crossings thật (2026-06-26) — Phase 1 number confirmation closed.

**Phase 4 đã chuẩn bị (PREP DONE — KHÔNG deploy):** `docker-compose.prod.yml` (no-publish ports, go2rtc, reid profile, ext net `dcnet-shared`, init.sql mount) + `db/init.sql` (schema dump Phase 0-3 → postgres-init, fix race) + `go2rtc.yaml` (RTSP→WS, ${CAM_*} env) + FDW `GET /api/auth/check` (forward_auth target — giải O9, gate `/live`+`/cam` sau khi bỏ Caddy basic_auth) + `.env.example` + `docs/ops/Caddyfile.post-flip.draft` + `docs/ops/2026-06-26-phase4-cutover-runbook.md` (A-D + O1-O9 resolve-at-deploy + verification 8pt + rollback + risks). **Deploy = session riêng qua skill `dcnet-deploy`:** clone /opt/camera-ai → dcnet-shared net → up prod → parity ≥1 ngày (so events DCNET vs camera-ai) → flip Caddy upstream `dashboard:8501→fall_detection_web:8090` (bỏ basic_auth, forward_auth /live) → decommission cũ (stop dashboard+collector DCNET; **mosquitto + postgres DCNET GIỮ**). ⚠️ Mosquitto = shared infra KHÔNG tắt; broker đọc-ké client-id riêng. ⚠️ requirements x86 reconcile (runbook): prod dùng `requirements.txt` +cpu wheels (nhỏ hơn CUDA `requirements.docker.txt`). VM-Opens O1-O9 resolve-at-deploy.

## Dual-counting test — camera-event vs YOLO (2026-06-30, PR #8, CHƯA merge)

Công cụ **test song song 2 bộ đếm người ra/vào (không định danh)** trên **trang chi tiết camera** (`/camera/{name}`), để so độ chính xác. Nhánh `feat/dual-counting-test` → [PR #8](https://github.com/dcnet-cloud/camera-ai/pull/8), code-complete + reviewed, **CHƯA deploy prod**. Spec [docs/superpowers/specs/2026-06-29-dual-counting-test-design.md](docs/superpowers/specs/2026-06-29-dual-counting-test-design.md); plans [2026-06-29-dual-counting-test.md](docs/superpowers/plans/2026-06-29-dual-counting-test.md) + [2026-06-30-counting-snapshots.md](docs/superpowers/plans/2026-06-30-counting-snapshots.md).

> **📝 Working notes (2026-06-30):** [docs/2026-06-30-dual-counting-yolo-tuning.md](docs/2026-06-30-dual-counting-yolo-tuning.md) — UI log 2 cột (Axis/YOLO) + popup, **tuning + thí nghiệm thực đo** engine đếm YOLO (model/imgsz/conf/vị-trí-vạch/ROI-crop trên 63 ảnh), menu giải pháp đếm tầng-ngoài cho cam không native detect, config live + việc còn lại. **Đọc file này để biết đang làm gì với YOLO counting.** Gate MQTT creds + `rtsp_url` cam thật ĐÃ giải trong session này (file §0).

- **2 block dưới "go2rtc Source"** (mỗi block VÀO/RA/ĐANG TRONG PHÒNG, chỉ hôm nay VN+7, poll 3s): 📷 **Camera (Axis)** = events `type='counter'` (do `event_collector` ghi từ MQTT line-crossing của camera); 🤖 **YOLO** = events `type='counter_yolo'`. **Cùng bảng `events`, phân biệt bằng `type`** → trang `/counting` cũ (lọc `type='counter'`) KHÔNG bị ảnh hưởng. Số = `COUNT` hôm nay + baseline.
- **Reset chung** (bảng `counting_baseline(cam_id PK, reset_ts, baseline)`): đặt người-đang-trong-phòng = N → cả 2 block `VÀO=N, RA=0`; chỉ áp khi `reset_ts` thuộc hôm nay; đếm tiếp các event `ts > reset_ts`. db: `counting_block`/`get|set_counting_baseline`. API (auth, `{name:path}` ở cuối): `GET /api/counting/camera/{name}`, `POST /api/counting/reset/{name}`, `POST /api/counting/yolo-config/{name}`.
- **Engine đếm YOLO ĐỘC LẬP** trong `monitor.py` (`start_counting`/`stop_counting`/`restart_counting`/`_counting_loop`, globals `counting_*`) — **KHÔNG nhét `_monitor_loop`** (cố ý: chạy được kể cả khi không bật fall_detection; tự mở RTSP full-FPS cho tracking). `model.track(persist=True, classes=[0])` + **vạch ngang `line_y%` + đoạn X `[x_start,x_end]%` + dead-band `min_disp%`** (chống rung); 1 model/cam. Chạy ⟺ `enabled AND yolo_counting.enabled`. Cấu hình per-cam = cột JSONB `cameras.yolo_counting {enabled,line_y,x_start,x_end,min_disp,invert}` (form thu gọn trong block YOLO). **Cần dep `lap`** (ultralytics tracker).
- **Snapshot lúc băng vạch** (1 ảnh/lượt): lưu `data/counting_snaps/` + `events.snapshot_path`; YOLO lưu frame engine; Axis = `event_collector` fetch go2rtc `/api/frame.jpeg` (httpx, **best-effort** — lỗi không gãy vòng MQTT). Serve `GET /api/counting-snap/{file}` (auth, path-validated); "Log hôm nay" + thumbnail trên trang (`db.counting_log_today`). Tự prune (`cleanup_counting_snaps`, random-chance). event_collector cần volume `fdw_data` + env `GO2RTC_INTERNAL_URL`/`COUNTING_SNAPS_DIR`; **dep `httpx`**.
- **Gates trước prod (ops, không phải bug):** (1) rebuild image `fall_detection_web` (bake `lap`) + `event_collector` (bake `httpx`); (2) đổi `cameras.rtsp_url` về IP camera thật (dev trỏ tạm `rtsp://go2rtc:8554/cam_door`); (3) **cấp MQTT broker creds** — dev hiện `Not authorized` nên block Camera(Axis) + snapshot Axis CHƯA nhận event (đây là nút "nhận event từ cam"). YOLO side chạy độc lập, không phụ thuộc điều này. Dev test: `docker compose up` → `/camera/Cửa cty HCM`.

## Read the AGENTS.md before editing either app

Each subproject has an `AGENTS.md` that encodes hard constraints. **These override general instincts — read the relevant one before changing code.** Highlights:

- **[`simple_ai_vision/AGENTS.md`](simple_ai_vision/AGENTS.md)** — Intentionally minimal. **DO NOT ADD** any of: a database, ORM, Redis, websockets, auth, background worker queues, RTSP/ffmpeg decoding, OpenCV pipelines, or object-detection models (YOLO/TF/PyTorch). Approved deps are only `fastapi`, `uvicorn`, `requests`, `paho-mqtt`, stdlib. Snapshots come **only** from go2rtc `/api/frame.jpeg?src={camera}`. No persistent background loops — the add-on stays idle until `POST /analyze`. Target: <150MB RAM idle.
- **[`fall_detection_web/AGENTS.md`](fall_detection_web/AGENTS.md)** — The opposite: a full multi-threaded app where heavy stacks (PyTorch, OpenCV, YOLO, Redis, SQLite) are *expected*. Prioritize performance, thread-safety (`threading.Lock` around shared state), and an OLED dark-mode UI in vanilla CSS (no SPA framework, no layout shift on hover).

## Commit / versioning conventions (from AGENTS.md)

- Commit and push after each source change.
- **Editing `simple_ai_vision/` → you MUST bump `version:` in [`simple_ai_vision/config.yaml`](simple_ai_vision/config.yaml)** (this is how HA detects add-on updates).
- **Editing `fall_detection_web/` → do NOT bump the add-on version** — it is a standalone app, unrelated to the add-on.

## Commands

### simple_ai_vision
```bash
cd simple_ai_vision
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000     # local run; in HA this is run.sh
docker build -t simple_ai_vision .              # add-on image build
```

### fall_detection_web
```bash
cd fall_detection_web
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt                 # pulls CPU torch from download.pytorch.org
uvicorn app:app --host 0.0.0.0 --port 8090      # default admin/admin on first run
```
Production runs under systemd with `--no-access-log` (see `fall_detection_web/README.md`). There is **no test suite, linter, or build step** in either project — verify changes by running the app.

## Architecture — simple_ai_vision

Single-file FastAPI app: [`app.py`](simple_ai_vision/app.py) (logic + all endpoints) and [`ui.py`](simple_ai_vision/ui.py) (the HTML served at `/`). Stateless request/response — **no database**. Config and event log live as JSON files in HA's `/data`:
- `/data/simple_ai_vision_config.json` — options (read via `read_options()` / written via `save_options()`)
- `/data/simple_ai_vision_events.jsonl` — append-only analyze log

Core flow is `POST /analyze` → `fetch_camera_snapshot` (go2rtc, with optional Frigate discovery) → `call_ai` (OpenAI-compatible vision, base64 data URL) → `keyword_matched` → `send_telegram` (`sendPhoto`) → `record_event`. Camera resolution prefers go2rtc `src` over a HA camera `entity_id`. Stream/camera discovery talks to go2rtc, Frigate, and the HA Supervisor (`SUPERVISOR_TOKEN`). MQTT publish is optional and must never become a required runtime dependency.

## Architecture — fall_detection_web

Multi-module FastAPI app. The data flow: **multiple RTSP/go2rtc cameras → threaded YOLOv8 person detection (CPU) → AI Vision scene verification (SAFE/EMERGENCY) → Telegram alert → optional incident video recording + Teldrive cloud upload → SQLite-backed events/recordings timeline + SOC dashboard.**

Module map:
- [`app.py`](fall_detection_web/app.py) — FastAPI routes (UI pages + `/api/*`), JWT cookie auth on every protected route via `Depends(auth.require_auth)`, Teldrive file proxy with disk caching + ETag/304, app lifespan that auto-starts the monitor on boot.
- [`monitor.py`](fall_detection_web/monitor.py) — the engine. A background monitor thread (`_monitor_loop`) per-camera captures frames, runs YOLO, and on detection calls `process_camera_verification` → AI → alert → record. All shared state goes through a module-level lock and `read_state()`/`set_state()`. Also handles go2rtc frame fetching, RTSP fallback, clip recording (copy-codec to spare CPU), thumbnails, and local-clip cleanup maintenance threads. `start_monitor`/`stop_monitor`/`restart_monitor` are the lifecycle entry points; config changes call `restart_monitor`.
- [`config.py`](fall_detection_web/config.py) — **3-tier config resolution: env/.env > SQLite `settings` table > `DEFAULT_CONFIG`.** Values are stored as TEXT and coerced on read (`_coerce`, `_INT_KEYS`/`_FLOAT_KEYS`/`_BOOL_KEYS`). `cameras` and `prompts` are JSON-encoded strings. Legacy `config.json` is auto-migrated into the DB once on startup. **Add a new setting in `DEFAULT_CONFIG` and (if env-overridable) `ENV_CONFIG_KEYS`, plus the right coercion set — not just in one place.**
- [`db.py`](fall_detection_web/db.py) — **PostgreSQL (psycopg v3, ConnectionPool)** for `incidents` (bảng fall-detection cũ tên `events`, đổi để tránh va chạm counting), `users`, và `settings`. DSN qua env `DATABASE_URL`/`DB_*`. Schema tạo trong `init_db` (tường minh, không migration framework). Bảng `recordings` = filter `incidents` theo cột video. Old incidents/images auto-pruned (7-day retention). Event images on disk in `data/event_images/`. (Phase 0 migration — xem `docs/superpowers/specs/2026-06-26-dcnet-platform-migration-design.md`.)
- [`ai.py`](fall_detection_web/ai.py) — OpenAI-compatible vision call (`verify_scene`) with a primary + fallback model, robust parsing of SSE / concatenated-JSON / thinking-tag responses, verdict parsing into `(result, description, raw)`, and Telegram `sendPhoto`.
- [`teldrive.py`](fall_detection_web/teldrive.py) — optional cloud upload to a Teldrive (Telegram-VFS) server. Auth supports a permanent **Static API Key** (sent as `Authorization: Bearer` and `X-API-Key`); files are organized into per-camera/date folders.
- [`redis_cache.py`](fall_detection_web/redis_cache.py) — **optional, fail-open** cache for dashboard/status/events/recordings responses. Every read/write is wrapped so a missing or broken Redis silently falls through to SQLite — never let a cache error break a request. Gated on `config["redis_enabled"]`.
- [`auth.py`](fall_detection_web/auth.py) — bcrypt password hashing + JWT (python-jose) session cookies. Secret comes from config `jwt_secret`, else a persisted `data/.secret_key`.
- `templates/` — Jinja2 pages (`index.html` is the SPA-like shell for dashboard/prompts/live/settings/tools; plus `cameras`, `camera_detail`, `login`). Vanilla CSS dark theme.

### Cross-cutting notes for fall_detection_web
- **Caching layers (all fail-open):** browser HTTP cache (`Cache-Control`/ETag/`Last-Modified`/304 on images & Teldrive proxy), local disk cache (`data/teldrive_cache/`), and Redis. When changing an endpoint's response shape, also invalidate/adjust its Redis cache key and bump caches via `db.invalidate_event_caches()` where events change.
- **Snapshot source priority:** go2rtc frame URL → RTSP direct (fallback). The RTSP URL must be the camera's own IP RTSP, *not* go2rtc's RTSP (see README camera table).
- Times are stored/displayed in Vietnam time (UTC+7) via `db.local_iso()`.
