# Luồng hoạt động hệ thống — DCNET Camera AI

Bộ sơ đồ **hợp nhất** mô tả toàn bộ nghiệp vụ của hệ thống giám sát camera AI: ứng dụng chính **`fall_detection_web`** (YOLO phát hiện người → AI Vision xác minh → cảnh báo Telegram → ghi hình + timeline) và add-on **`simple_ai_vision`** (gọi theo trigger Home Assistant). Mở [`index.html`](index.html) để xem gallery; mỗi sơ đồ là HTML độc lập, có toggle sáng/tối và xuất PNG/SVG.

> 3 sơ đồ AI (`ai-verification-sequence`, `ai-data-pipeline`, `ai-fault-tolerance`) là **phần chuyên sâu dành riêng cho luồng hoạt động của AI**.

## 1. Kiến trúc hệ thống — [`system-architecture.html`](system-architecture.html)
4 vùng triển khai: **Camera LAN**, **VPS · fall_detection_web** (go2rtc, Capture Thread, Monitor Loop, YOLOv8, AI Client, SQLite WAL, Redis fail-open, Web UI), **Cloud** (AI Vision API primary+fallback, Telegram, Teldrive), và **Home Assistant Add-on · simple_ai_vision** dùng chung go2rtc/AI/Telegram. Ưu tiên `go2rtc frame.jpeg`, fallback RTSP.

## 2. Luồng vận hành Monitor — [`operational-workflow.html`](operational-workflow.html)
Swimlane 5 làn: Camera & Stream → Capture & YOLO → AI Verification → Cảnh báo & Ghi hình → Lưu trữ & Timeline. `frame_skip` chạy YOLO 1/N khung; chỉ `verify_scene` khi có person + quá `verify_interval`; EMERGENCY mới cảnh báo (chặn bởi `alert_cooldown`), SAFE chỉ `log_event`; ghi clip thread riêng (`record_cooldown`).

## 3. 🤖 AI · `verify_scene` — [`ai-verification-sequence.html`](ai-verification-sequence.html)
Trình tự gọi Vision API: `POST /chat/completions` (text + image_url base64, `max_tokens=1000`, timeout 120s, prompt theo `prompt_id` camera) → lỗi 5xx/timeout thì tự `fallback_vision_model` → `response_ai_content` parse SSE/JSON → `strip_thinking_content` → `(result, description, raw)` → EMERGENCY + quá cooldown thì `sendPhoto` → `log_event`.

## 4. 🤖 AI · luồng dữ liệu — [`ai-data-pipeline.html`](ai-data-pipeline.html)
Dataflow 5 chặng: Khung hình → Chuẩn bị → Vision API → Parse → Đầu ra. Snapshot → base64 data URL; prompt theo `prompt_id`/`verify_prompt`; gửi `vision_model`, lỗi → `fallback_vision_model`; `content` → strip thinking → parse verdict → phân nhánh SAFE/EMERGENCY (chỉ EMERGENCY gửi Telegram, mọi case `log_event`).

## 5. 🤖 AI · cơ chế chịu lỗi — [`ai-fault-tolerance.html`](ai-fault-tolerance.html)
Circuit breaker: AI Cloud hoặc Teldrive lỗi liên tiếp ≥3 lần → tạm ngưng theo thang backoff (60s → 300s → 900s → 3600s) + Telegram cảnh báo; lúc ngưng AI trả mặc định `SAFE`. Một lần thành công → reset bộ đếm và báo khôi phục. Tách riêng `ai_suspended` và `upload_suspended`.

## 6. Ghi hình & lưu trữ Teldrive — [`recording-storage.html`](recording-storage.html)
Có người + bật ghi (ngoài cooldown) → quay clip `go2rtc stream.mp4` (copy codec, dự phòng OpenCV) → `upload_event_video` → ghi `teldrive_video_uploaded` + thumbnail. `maintenance loop` (600s) retry clip lỗi và dọn local clip đã upload.

## 7. Add-on Simple AI Vision — [`simple-ai-vision.html`](simple-ai-vision.html)
Home Assistant POST `/analyze {camera}` → snapshot (go2rtc, fallback Frigate) → AI Vision với prompt profile → `matched_keyword` (regex) → khớp thì `sendPhoto` + ghi `sent`, không khớp ghi `no_match`. Khác `fall_detection_web`: không YOLO, không vòng lặp, phân loại keyword thay vì SAFE/EMERGENCY.

## 8. Mô hình dữ liệu SQLite — [`data-model.html`](data-model.html)
3 bảng độc lập: `events` (kèm `ai_result/ai_raw/ai_response` và các trường `teldrive_*`), `users` (bcrypt hash), `settings` (key/value). Pruning: giữ tối đa 5000 events, xóa ảnh quá 24h, event quá 7 ngày.

---

## Nguồn gốc & hợp nhất (provenance)
Bộ này hợp nhất 2 nguồn:
- **Sẵn có trong camera-ai** (giữ + bổ sung): `system-architecture`, `operational-workflow`, `ai-verification-sequence`, `ai-data-pipeline` — vốn chi tiết phần nội bộ `fall_detection_web` (Capture Thread, Monitor Loop, AI Client, primary/fallback tách riêng).
- **Đem từ camera-check sang** (4 sơ đồ mới): `ai-fault-tolerance`, `recording-storage`, `simple-ai-vision`, `data-model`.

Cập nhật khi merge:
- `system-architecture`: thêm vùng **Home Assistant Add-on** (`ha` + `saiv`) nối go2rtc/AI/Telegram + card Simple AI Vision.
- `operational-workflow`: thêm note loop khi không có người, nhánh SAFE chỉ log, `telegram_sent`, tham chiếu backoff.
- `ai-verification-sequence`: thêm note chọn prompt theo camera + tham số request trên message gọi model chính.
- `ai-data-pipeline`: giữ nguyên (đã là bản đầy đủ hơn).

Hai thư mục `camera-ai/docs/diagrams` và `camera-check/docs/diagrams` được **mirror đồng nhất** (cùng tên file + nội dung); codebase 2 repo giống hệt nên 1 bộ sơ đồ dùng chung.

**Chỉnh sửa:** sửa JSON-IR trong [`src/`](src/) rồi chạy lại renderer (`node renderers/<type>/render-<type>.mjs src/<name>.json <name>.html`) và `python3 scripts/build_gallery.py manifest.json .`.
