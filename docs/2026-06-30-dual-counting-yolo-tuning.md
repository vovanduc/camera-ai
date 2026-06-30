# Dual-counting — UI 2 cột + tuning YOLO đếm người (2026-06-30)

> **Working notes của session 2026-06-30.** Ghi lại: thay đổi UI trang chi tiết camera, hành trình tuning engine đếm YOLO, **kết quả thí nghiệm thực đo** (63 ảnh snapshot), menu giải pháp tầng-ngoài, config live hiện tại, và việc còn lại. Liên quan: tính năng [dual-counting test (PR #8)](superpowers/specs/2026-06-29-dual-counting-test-design.md) — đã merge `main`.

## 0. Bối cảnh đầu session

- Stack dev chạy `docker compose` (postgres + fall_detection_web :8090 + go2rtc + event_collector). Branch `main` (PR #8 dual-counting đã merge).
- **2 gate ops trong CLAUDE.md đã GIẢI lúc vào session:**
  - **MQTT broker creds đã thông** — `event_collector` nhận counter event từ Axis, ghi `events(type='counter')` đều (trước đó `Not authorized`).
  - **`cameras.rtsp_url` đã trỏ camera Axis thật** `rtsp://root:...@192.168.100.47:554/axis-media/media.amp` (trước trỏ tạm `rtsp://go2rtc:8554/cam_door`). `go2rtc_src=cam_door` vẫn pull từ cam này.
- **Image đã rebuild** bake `lap` (FDW) + `httpx` (event_collector) — gate (1) giải. Recreate container.
- **Còn treo:** `ai_api_key` rỗng → AI Vision verify chưa chạy → log spam `Missing required config: ai_api_key` + sau 5 fail monitor tự suspend AI (by design). YOLO/counting độc lập, không ảnh hưởng.

## 1. Thay đổi UI — `fall_detection_web/templates/camera_detail.html`

Khối "Log hôm nay" (`#countLog`) dưới 2 block đếm:

1. **Tách 2 cột** (CSS `.count-log` đổi từ flex 1-cột → `grid-template-columns:1fr 1fr`):
   - Trái 📷 **Camera (Axis)** = events `source=axis` (`type='counter'`)
   - Phải 🤖 **YOLO (máy local)** = events `source=yolo` (`type='counter_yolo'`)
   - Mỗi cột: header sticky + list cuộn riêng (`max-height:320px`), rỗng → "Chưa có lượt nào hôm nay."
   - Responsive: `@media (max-width:640px)` → 1 cột.
2. **Click thumb → popup modal** thay `window.open`. Reuse `showViewer(title,url,"image")` + dialog `#viewerDialog` có sẵn (thumb gắn `data-snap`/`data-title`, gán `onclick` sau render).
3. **Click backdrop → tự đóng** popup: `viewerDialog.addEventListener("click", e => { if (e.target===e.currentTarget) e.currentTarget.close() })`. Esc vẫn đóng native.

> ⚠️ Template **bake trong image** (compose chỉ mount `fdw_data`, không mount source) → sửa template phải `docker compose build fall_detection_web && up -d`. Không hot-reload.

## 2. Engine đếm YOLO — cơ chế (nhắc lại từ PR #8)

- Gate chạy: `db.list_yolo_counting_cameras()` = `enabled=true AND (yolo_counting->>'enabled')::bool=true`. Lúc đầu `yolo_counting={}` → engine không start (YOLO=0). Bật qua form trang camera → `POST /api/counting/yolo-config/{name}` → set cột JSONB + `monitor.restart_counting()`.
- `_counting_loop` (monitor.py ~1257): **mở `rtsp_url` TRỰC TIẾP** (không qua go2rtc), `cv2.VideoCapture` full-FPS, `model.track(persist=True, classes=[0])`, vạch ngang `line_y%` + đoạn X `[x_start,x_end]%` + dead-band `min_disp%`. Crossing → `db.insert_counting_event(..., 'yolo', snapshot_path=...)` + lưu frame.
- Đọc `confidence`, `yolo_imgsz`, `yolo_model` từ `read_config()` lúc start loop → đổi 3 setting này **phải restart_counting** mới áp.
- `confidence`/`yolo_imgsz` là **global** (dùng chung fall-detection monitor). Không có env override (`.env` trống các key này) → settings table thắng.

## 3. Hành trình tuning + chẩn đoán

| Bước | Vạch | conf/imgsz | Kết quả |
|---|---|---|---|
| Bật lần đầu | y=50, X0-100 | 0.5 / 416 | Đếm được khi người đi GIỮA phòng (box to). Lệch ~47s vs Axis. |
| Calibrate về cửa | y=38, X47-72 | 0.5 / 416 | **0 crossing** — người ở cửa quá nhỏ/xa, YOLO không detect (people=0 dù Axis đếm). |
| Tăng nhạy | y=38 | **0.3 / 640** | Vẫn 0 — cửa quá xa cho yolov8n. |
| **Chốt** | **y=52, X30-80** | 0.3 / 640 | Lối đi giữa phòng — vùng người to/chắc (theo histogram bên dưới). |

**Gốc rễ (xem ảnh thực):** vạch line-crossing của Axis nằm ở **cửa kính** (giữa-trên khung, nhìn xuyên ra thang máy "Lầu 2") — **rất xa camera** → người ở đó nhỏ xíu. Đây là **hình học camera**, không phải bug. Snapshot Axis (event_collector fetch go2rtc lúc MQTT bắn, có latency) cho thấy người lúc đó đã đi sâu vào phòng → TO, dễ detect.

## 4. Thí nghiệm THỰC ĐO (63 ảnh `*axis*.jpg` trong `data/counting_snaps/`)

Script: `scratchpad/yolo_probe.py`, `yolo_probe2.py` (chạy trong container, có ultralytics + net tải model).

### A) Ma trận model × imgsz × conf — detect rate trên 63 frame có người

| config | detect | median CY | box H | conf |
|---|---|---|---|---|
| yolov8n / 416 / 0.5 (cũ) | 65% | 61% | 40% | 0.76 |
| yolov8n / 640 / 0.3 | 87% | 60% | 31% | 0.80 |
| yolov8n / 960 / 0.25 | 92% | 54% | 30% | 0.83 |
| yolov8s / 640 / 0.3 | 90% | 58% | 30% | 0.84 |
| **yolov8s / 960 / 0.25** | **95%** | 58% | 30% | 0.85 |

→ **Model KHÔNG phải vấn đề.** YOLO detect người 87-95% trong các frame này, conf 0.8+. Người được detect ở **y≈58%** (giữa-dưới), KHÔNG ở cửa (y~38%).

### B) Histogram CY (người detect nằm ở y% nào) — bimodal

```
y 30-40%: 12    y 40-50%: 28 ←đỉnh    y 50-60%: 8
y 60-70%: 31 ←đỉnh   y 70-80%: 13     y 80-90%: 1     median=58%
```
→ Luồng người transit qua **dải y 40-70%** (lối đi). Vạch nên đặt ~50-58%, KHÔNG 38%.

### C) ROI crop+upscale vùng cửa (đếm ngay tại choke xa)

ROI cửa `x[45,75]% y[8,45]%`, crop + upscale 3x rồi detect:

```
full-frame  thấy người Ở CỬA : 43%
ROI crop+3x thấy người Ở CỬA : 56%   (+13%)
```
→ Crop+phóng to **cải thiện rõ** detect người xa. Không hoàn hảo (cửa này cực xa + kính phản chiếu) nhưng là **kỹ thuật chuẩn cho cam choke xa**.

## 5. Kết luận — đếm tầng-ngoài cho cam KHÔNG native detect

**Chứng minh bằng số:** YOLO tầng-ngoài đủ tốt để đếm, **chìa khóa = đặt vạch ở vùng người to/gần** (lối đi), không phải choke xa. Thứ tự đòn bẩy recall: **vạch đúng chỗ (rẻ nhất) > imgsz↑ > model↑ > conf↓**.

**Cam mà choke BẮT BUỘC ở xa** (không có chỗ gần đặt vạch) → **ROI crop+upscale** là giải pháp đúng.

### Menu giải pháp tầng-ngoài

| Giải pháp | Hiệu quả (đo) | Chi phí | Trạng thái |
|---|---|---|---|
| Vạch đúng chỗ detect (y~52%) | 65%→95% | 0 (config) | ✅ đã áp |
| imgsz 416→960 | +27% | CPU 2-3x | ✅ test |
| model n→s | +5-8% | CPU 2x | ✅ test |
| conf 0.5→0.25 | +người xa | ↑false-pos | ✅ test (đang 0.3) |
| ROI crop+upscale (choke xa) | cửa 43→56% | **cần code** | ⬜ chưa build |
| Zone/polygon thay line | robust góc nghiêng | code | ⬜ |
| Tracker bytetrack vs botsort | ID ổn định | config | ⬜ |
| Model head-detection (đám đông/xa) | recall người nhỏ | model riêng | ⬜ |

## 6. Config LIVE hiện tại (cam "Cửa cty HCM")

- `cameras.yolo_counting = {enabled:true, line_y:52, x_start:30, x_end:80, min_disp:6, invert:false}`
- `settings.confidence = 0.3`, `settings.yolo_imgsz = 640`, `yolo_model = yolov8n.pt`
- Engine chạy (đã restart). Chờ người đi qua dải y=52% → ghi `counter_yolo` + thumbnail cột phải.
- ⚠️ Probe offline dùng yolov8s/960 OK, nhưng **full-FPS live nặng CPU** — live nên giữ n@640, hoặc giảm FPS xử lý.

## 7. Việc còn lại / quyết định mở

1. ✅ **ROI zoom-zone per-cam (DONE 2026-06-30)** — config `yolo_counting` thêm `{roi_enabled,roi_x1,roi_y1,roi_x2,roi_y2,imgsz}`. `_counting_loop` crop vùng ROI trước `model.track`; **line-crossing tính theo toạ độ CROP** → khi bật ROI, `line_y`/`x_start`/`x_end` = **% TRONG ROI** (đặt vạch trong ROI: `roi_y1<line_y<roi_y2`, nếu không → 0 crossing, lặp lại bẫy y=38 §3). Snapshot vẫn lưu **full frame** (context). Form trong block YOLO + validate API (`x1<x2 && y1<y2`). Backward-compat: roi off = byte-identical path cũ. Verify: rebuild + restart → engine boot `imgsz=960 roi=[40,5,80,50]`, loop chạy không crash; revert về baseline (imgsz=0, roi off) tránh peg CPU.
   - ⚠️ **`upscale` thủ công ĐÃ BỎ (cố ý).** YOLO letterbox input về `imgsz` trước inference → cv2-upscale crop rồi YOLO co lại = no-op, chỉ tốn CPU. **Đòn bẩy thật = `imgsz` so với kích thước crop**, nên expose **per-cam `imgsz`** (0=dùng global) thay vì upscale factor — knob này sống tới network. Gộp luôn §7.3 (phần imgsz).
   - ⚠️ **Số 56% (§4C) CHƯA tái đo trên path live** — probe cũ ở scratchpad đã mất (session-specific). Cơ chế ROI đúng; cần đi qua vạch thật để confirm recall. Model vẫn global (chỉ imgsz per-cam).
2. **Zone/polygon counting** thay line ngang — robust góc nghiêng.
3. ✅ **Per-cam imgsz DONE** (cùng item 1). ⬜ **Per-cam model** vẫn chưa (cam khó dùng `s`, cam dễ dùng `n`) — `yolo_model` còn global.
4. **Nhập `ai_api_key`** (+ telegram token) → AI Vision verify (fall/stroke) chạy thật, hết log spam. Model vision đề xuất: `claude-opus-4-8` qua router 9router (OpenAI-compat).
5. **"Nhận diện người ngoài cty"** = face recognition (ArcFace/InsightFace + pgvector), **KHÔNG phải vision LLM** — dùng lại `services/reid_worker/` (Phase 2, shelved vì license non-commercial + cam placement). Dự án riêng.

## 8. Lệnh dev hữu ích

```bash
# rebuild + recreate FDW (sau khi sửa template/code)
docker compose build fall_detection_web && docker compose up -d fall_detection_web
# DB (user/db = dcnet/dcnet)
docker exec camera-ai-postgres-1 psql -U dcnet -d dcnet -c "select type,direction,count(*) from events group by 1,2"
# bật/đổi vạch YOLO (auth qua cookie sau POST /auth/login admin/admin)
curl -b cookie -X POST 'http://localhost:8090/api/counting/yolo-config/Cửa cty HCM' -d '{"enabled":true,"line_y":52,...}'
# tail engine
docker logs -f fall_detection_web 2>&1 | grep -E '\[COUNT\]|\[YOLO\]'
```
