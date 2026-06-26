# AGENTS.md (Fall Detection Web)

## Project Overview

This project is a standalone, advanced AI-powered Camera Monitoring & Fall Detection Web Application.
Always use UTF-8.

Workflow:
Multiple Camera Streams (RTSP / go2rtc)
→ Multi-threaded local YOLOv8 Person Detection (CPU)
→ AI Vision Scene Validation (OpenAI compatible API / Gemini)
→ INCIDENT VERDICT (SAFE / EMERGENCY)
→ Instant Telegram Notification with Photo
→ Incident Video Recording & Cloud Upload (Teldrive)
→ Incident Timeline, Video Recordings Hub, and SOC Dashboard

---

## Core Principles

ALWAYS prioritize:
* **High Performance & Reactivity:** The dashboard and video streams should be fast, highly responsive, and load instantaneously.
* **Robust Multi-threading:** Camera frame capture, YOLO inference, AI validation, and Teldrive uploads must run on isolated threads to prevent bottlenecks.
* **Extensibility:** The project is a standalone web app and is actively expanding to include modern web features, richer dashboards, and state-of-the-art caching.
* **Beautiful UX/UI:** Implement premium, Harmonious Dark Mode (OLED) aesthetics with smooth transitions, custom widgets, and responsive layouts.

---

## Approved Tech Stack

Allowed libraries and tools:
* **Core:** FastAPI, Uvicorn, Jinja2 Templates, python-multipart.
* **AI & Computer Vision:** PyTorch, torchvision, opencv-python, ultralytics (YOLOv8).
* **Database & Auth:** PostgreSQL (psycopg v3, psycopg-pool) — bảng `incidents` (cũ: `events`), `users`, `settings`; bcrypt, python-jose. DSN qua env `DATABASE_URL`. (Phase 0: đã migrate từ SQLite sang Postgres — xem `docs/superpowers/specs/2026-06-26-dcnet-platform-migration-design.md`.)
* **Cloud Integration:** Teldrive, Requests.
* **Caching & State Management:** Redis (for session caching, stream state, and performance optimization), local Disk Cache, and Browser HTTP Cache headers.
* **System Utilities:** psutil.

---

## Caching Strategy

To ensure seamless, smooth loading across the dashboard:
* **Browser HTTP Cache:** Leverage strong Cache-Control headers (`private, max-age=86400, immutable`), ETags, and Last-Modified timestamps for static assets and local static images.
* **Local Disk Cache:** Cache downloaded media (e.g. Teldrive image thumbnails) on disk (`data/teldrive_cache/`) to avoid repeatedly calling external APIs.
* **Redis Cache:** Approved for caching session metadata, temporary stream statuses, camera availability state, and high-frequency dashboard analytics.

---

## Coding Style

* Clean, modular Python functions.
* Thread-safe shared data access using `threading.Lock` and locks.
* Explicit error logging for all API timeouts, database transactions, and stream reconnections.
* HTML templates should use raw vanilla CSS styled to match the design system (`design-system/MASTER.md`). No layout shifts on hovers.

---

## GitHub Updates

After each source code update, commit the changes and push them to GitHub.
Note: Since fall_detection_web is a standalone web application, you do NOT need to increase the add-on version in `simple_ai_vision/config.yaml` when modifying fall_detection_web files.
