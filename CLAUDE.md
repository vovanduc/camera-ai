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
