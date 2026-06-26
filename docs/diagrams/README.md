# docs/diagrams — Sơ đồ nghiệp vụ DCNET Camera AI

Bộ sơ đồ kỹ thuật/nghiệp vụ của dự án, sinh bằng skill **`dcnet-diagram`**. Mỗi `.html` tự chứa (toggle sáng/tối, xuất PNG/SVG) và render lại được từ JSON-IR trong [`src/`](src/).

👉 Mở **[`index.html`](index.html)** để xem gallery. Giải thích chi tiết: **[`EXPLAINER.md`](EXPLAINER.md)**.

## Phạm vi
Hệ thống gồm 2 add-on dùng chung tầng AI Vision + Telegram + go2rtc:
- **`fall_detection_web/`** — web app tự host: YOLO phát hiện người → AI Vision xác minh → Telegram → ghi hình/timeline.
- **`simple_ai_vision/`** — add-on Home Assistant: gọi `/analyze` theo trigger, phân loại bằng keyword.

## 8 sơ đồ

| # | Sơ đồ | Loại | Nội dung |
|---|---|---|---|
| 1 | [system-architecture](system-architecture.html) · [src](src/system-architecture.json) | architecture | Toàn cảnh 4 vùng: Camera LAN, VPS fall_detection_web (Capture/Monitor/YOLO/AI Client/SQLite/Redis), Cloud (AI/Telegram/Teldrive), HA Add-on simple_ai_vision. |
| 2 | [operational-workflow](operational-workflow.html) · [src](src/operational-workflow.json) | workflow | Swimlane vòng lặp Monitor: capture → YOLO → verify → verdict → cảnh báo/ghi clip/timeline. |
| 3 | 🤖 [ai-verification-sequence](ai-verification-sequence.html) · [src](src/ai-verification-sequence.json) | sequence | Trình tự `verify_scene`: model chính → fallback → parse verdict → cảnh báo + log. |
| 4 | 🤖 [ai-data-pipeline](ai-data-pipeline.html) · [src](src/ai-data-pipeline.json) | dataflow | Đường đi dữ liệu ảnh: base64 + prompt → Vision API → parse SSE/JSON/think → SAFE/EMERGENCY. |
| 5 | 🤖 [ai-fault-tolerance](ai-fault-tolerance.html) · [src](src/ai-fault-tolerance.json) | lifecycle | Circuit breaker: lỗi liên tiếp ≥3 → backoff 60→3600s + cảnh báo, tự hồi phục. |
| 6 | [recording-storage](recording-storage.html) · [src](src/recording-storage.json) | workflow | Ghi clip go2rtc/OpenCV → upload Teldrive → bảo trì/retry/dọn local. |
| 7 | [simple-ai-vision](simple-ai-vision.html) · [src](src/simple-ai-vision.json) | sequence | Trình tự `/analyze`: HA trigger → snapshot → AI Vision → keyword → Telegram. |
| 8 | [data-model](data-model.html) · [src](src/data-model.json) | erd | Bảng SQLite: events, users, settings. |

🤖 = nhóm chuyên sâu về luồng hoạt động của AI.

## Cấu trúc thư mục
```
docs/diagrams/
├── index.html          # gallery (build từ manifest.json)
├── README.md           # file này
├── EXPLAINER.md        # giải thích từng sơ đồ + provenance
├── manifest.json       # danh sách sơ đồ cho gallery
├── <name>.html         # 8 sơ đồ đã render
└── src/<name>.json     # 8 nguồn JSON-IR (sửa rồi render lại)
```

## Cập nhật sơ đồ
1. Sửa JSON-IR trong `src/<name>.json`.
2. Render lại: `node renderers/<type>/render-<type>.mjs src/<name>.json <name>.html` (type ∈ architecture|workflow|sequence|dataflow|lifecycle|erd).
3. Dựng lại gallery: `python3 scripts/build_gallery.py manifest.json .`

Màu/font/brand đọc từ `brand.config.json` (skill root). Giữ sơ đồ đồng bộ khi luồng code thay đổi.
