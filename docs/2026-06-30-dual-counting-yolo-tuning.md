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

- `cameras.yolo_counting = {enabled:true, line_y:52, x_start:30, x_end:80, min_disp:6, invert:false}` (model/imgsz/conf rỗng → dùng global)
- `settings.confidence = 0.3`, `settings.yolo_imgsz = 640`, `yolo_model = yolov8n.pt` (global default; per-cam override trong `yolo_counting`)
- Engine chạy (đã restart). Chờ người đi qua dải y=52% → ghi `counter_yolo` + thumbnail cột phải.
- ⚠️ Probe offline dùng yolov8s/960 OK, nhưng **full-FPS live nặng CPU** — live nên giữ n@640, hoặc giảm FPS xử lý.

## 6b. Chẩn đoán Axis-vs-YOLO không khớp (2026-06-30, từ data + ảnh thật)

**KHÔNG phải bug.** Tỉ lệ in/out giống hệt: Axis 114/87=**1.31**, YOLO 17/13=**1.31** → logic crossing đúng, YOLO chỉ là mẫu ~15%. Lệch vì **2 vạch ở 2 vị trí vật lý khác nhau** (xác nhận bằng snapshot): 📷 Axis đếm tại **cửa kính** (xa); 🤖 YOLO đếm tại **vạch giữa phòng y=52%** (đặt đó vì cửa quá xa). Người qua cửa trước → tới vạch giữa phòng sau vài giây (lag ~1 phút), nhiều người không bao giờ chạm vạch giữa → YOLO 15%. ⚠️ Số hôm nay **bẩn** (restart engine nhiều lần khi test). ⚠️ Axis chưa chắc ground-truth (cụm in/out liên tục = có thể nhiễu). ⚠️ RTSP camera **burn sẵn overlay analytics** vào stream → YOLO detect trên frame đã có box/icon.

**Quyết định (user, 2026-06-30): Option 1 — 2 cam SO CÙNG VẠCH ở cửa.** Ràng buộc: **không được detect người ngoài cửa** (lobby "Lầu 2" sau kính). Cách làm: bật ROI crop vùng cửa (loại vùng ngoài kính khỏi detect) + đặt vạch YOLO trùng vạch Axis. Đánh đổi đã biết: choke xa, recall ~56% — cần yolov8s + imgsz cao.

## 6c. Preview calibrate vạch (DONE 2026-06-30) — `GET /api/counting/preview/{name}`

Tuning trước đây **mù** (gõ số, chờ crossing). Thêm **preview trực quan**: `monitor.counting_preview()` lấy 1 frame (go2rtc, fallback RTSP) → chạy YOLO 1 lần với **đúng model/imgsz/conf per-cam** → vẽ **ROI (xanh lá) + vạch (cam) + box người (đỏ, chấm xanh=trong x-range sẽ đếm / vàng=ngoài)** + header `model/imgsz/conf/people` → JPEG. Endpoint GET query-param (read-only, KHÔNG lưu DB, KHÔNG restart loop); cfg dựng qua `_build_yolo_cfg` (dùng chung POST). Form: nút **"Xem trước vạch"** → `showViewer` popup. **Workflow calibrate cửa:** kéo ROI bao cửa, nâng `roi_y1` tới khi người ngoài kính KHÔNG còn bị box, hạ vạch về ngưỡng cửa, tăng imgsz/model tới khi người ở cửa được box ổn định. Verify: endpoint trả JPEG 200 có overlay đúng hình học.

## 7. Việc còn lại / quyết định mở

1. ✅ **ROI zoom-zone per-cam (DONE 2026-06-30)** — config `yolo_counting` thêm `{roi_enabled,roi_x1,roi_y1,roi_x2,roi_y2,imgsz}`. `_counting_loop` crop vùng ROI trước `model.track`; **line-crossing tính theo toạ độ CROP** → khi bật ROI, `line_y`/`x_start`/`x_end` = **% TRONG ROI** (đặt vạch trong ROI: `roi_y1<line_y<roi_y2`, nếu không → 0 crossing, lặp lại bẫy y=38 §3). Snapshot vẫn lưu **full frame** (context). Form trong block YOLO + validate API (`x1<x2 && y1<y2`). Backward-compat: roi off = byte-identical path cũ. Verify: rebuild + restart → engine boot `imgsz=960 roi=[40,5,80,50]`, loop chạy không crash; revert về baseline (imgsz=0, roi off) tránh peg CPU.
   - ⚠️ **`upscale` thủ công ĐÃ BỎ (cố ý).** YOLO letterbox input về `imgsz` trước inference → cv2-upscale crop rồi YOLO co lại = no-op, chỉ tốn CPU. **Đòn bẩy thật = `imgsz` so với kích thước crop**, nên expose **per-cam `imgsz`** (0=dùng global) thay vì upscale factor — knob này sống tới network. Gộp luôn §7.3 (phần imgsz).
   - ⚠️ **Số 56% (§4C) CHƯA tái đo trên path live** — probe cũ ở scratchpad đã mất (session-specific). Cơ chế ROI đúng; cần đi qua vạch thật để confirm recall. Model vẫn global (chỉ imgsz per-cam).
2. **Zone/polygon counting** thay line ngang — robust góc nghiêng.
3. ✅ **Per-cam model/imgsz/conf DONE (2026-06-30)** — **mọi knob đếm giờ nằm trong `yolo_counting`** (dễ tuỳ chỉnh từng cam): `model`/`imgsz`/`conf` override global khi set (rỗng/0 = dùng global từ `settings`). `_counting_loop` resolve per-cam ở đầu loop. Cam khó → `yolov8s@960`, cam dễ → `yolov8n@640` tiết kiệm CPU. **Model có allowlist cứng** (`_YOLO_MODEL_ALLOWLIST` trong app.py — `YOLO(name)` nạp file nên chặn path/URL tuỳ ý; verify: `model="../../etc/passwd"` → 400). Form: dropdown model + 2 ô imgsz/conf trong block YOLO. Đổi 3 knob này vẫn cần lưu (→ `restart_counting`).
4. ✅ **AI Vision verify ĐÃ CHẠY (2026-06-30, session sau) — xem §9.** Dùng Gemini qua 9router. (Telegram token vẫn chưa nhập → alert chưa gửi.)
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

---

## 9. AI Vision verify — config + prompt + crop + vùng loại trừ (2026-06-30, session sau)

Session này bật được AI Vision verify (chạy độc lập engine đếm), thêm prompt nhận diện người, và 2 cơ chế xử-lý-ảnh-trước-khi-gửi-AI (crop + vùng loại trừ).

### 9.1 Kết nối 9router (OpenAI-compat proxy)

- **App trong Docker → `ai_base_url` PHẢI `http://host.docker.internal:20128/v1`**, KHÔNG `localhost`. Trong container `localhost` = chính container → `Connection refused`. (Chứng minh: `host.docker.internal:20128/v1/models`→200; `localhost`→refused.)
- 9router là **proxy gom nhiều provider**; `/v1/models` liệt **398 model** nhưng chỉ provider **đã connect creds** mới chạy — model chưa connect → `404 No active credentials for provider: X`. Dashboard 9router: `http://localhost:20128` (pass mặc định `123456`), `/api/providers`→`{connections:[]}` lúc trống. Connect provider trong dashboard (OAuth subscription hoặc API key).
- Prefix provider: `gc/`=gemini-cli · `cc/`=claude subscription · `gh/`=github copilot · `openai/` · `anthropic/` · `ag/`=antigravity · `cu/`=cursor · `if/`/`qw/`=qwen-VL...
- **Đang dùng:** Gemini CLI (`gc/`). Model `gc/gemini-2.5-flash` (vision OK, test ảnh thật đọc đúng giới tính/áo).

### 9.2 Key config AI (3-tier env>settings>default)

- **`vision_model`** (KHÔNG phải `ai_model`) = model chính. Default cũ `gh/oswe-vscode-prime` = **code model, KHÔNG vision + github chưa connect** → đổi `gc/gemini-2.5-flash`.
- **`fallback_vision_model`** = fallback **chỉ khi model chính raise** (ai.py:251-254), KHÔNG chạy song song.
- **`ai_api_key` BẮT BUỘC khác rỗng** — `verify_scene→require_config([...,'ai_api_key'])` raise nếu rỗng, **dù 9router không kiểm key**. Set dummy `9router-dummy`.
- **max_tokens hardcode 1000** (ai.py:207) — đủ. ⚠️ Gemini 2.5 flash là **model thinking**, đốt `reasoning_tokens`; max_tokens nhỏ (vd 80) → output rỗng/cụt, mất keyword SAFE/EMERGENCY.
- 9router trả **SSE stream** (`data: {...}` chunks) — `ai.py` parse sẵn (robust SSE/concat-JSON/thinking-tag). OK.
- ⚠️ Mỗi verify ~**4.3k input tokens** (ảnh). Gemini CLI = quota subscription → bật nhiều cam đồng thời dễ nghẽn. 1 model/cam.

### 9.3 Cơ chế prompt (đã có sẵn, làm rõ)

- **`verify_prompt`** = prompt DEFAULT toàn cục (fall detection, ép 2 dòng SAFE/EMERGENCY). Ở Settings "Prompt xác minh mặc định".
- **`prompts`** = list `{id,title,content}` (settings JSON). Gán per-cam qua **`cameras.prompt_id`**. Cam không gán → rơi về `verify_prompt`. Quan hệ **1 cam = 1 prompt** (prompt_id 1 giá trị). Model **global, không per-prompt**. UI: tab **Prompts** (Add prompt).
- Thêm prompt **"Nhận diện người vào"** (id `person-id-entry`): mô tả Giới tính/Đeo kính/Áo màu, mỗi ý 1 dòng. Gán cho cam "Cửa cty HCM". ⚠️ Prompt này KHÔNG có dòng SAFE/EMERGENCY → app coi mọi cảnh SAFE → **không alert Telegram** (chuyển cam từ "phát hiện ngã" sang "nhận diện người"). Hướng/in-out KHÔNG suy ra được từ 1 ảnh tĩnh (đó là việc engine đếm line-crossing).

### 9.4 `verify_crop` — cột JSONB mới trên `cameras` (feature session này)

`cameras.verify_crop = {enabled, padding, ignore_zones}`. Mirror pattern `yolo_counting`. **Chỉ tác động ảnh đưa AI ở `_monitor_loop` (fall-detection verify), KHÔNG đụng engine đếm.**

- **Crop vào người:** khi `enabled`, lấy bbox người **conf cao nhất** từ YOLO + **padding** (fraction của w/h bbox, clamp biên) → lưu **FILE RIÊNG** `data/camera_{i}_aicrop.jpg` chỉ đưa `verify_scene`. **`verify_path` (log incident + Telegram + snapshot live) giữ FULL frame.** Lý do tách file: tránh crop làm hỏng ảnh log/alert (advisor). Crop giúp AI đọc chi tiết (kính/giới tính) tốt hơn full-frame khi người nhỏ.
- **`ignore_zones`** = list `[[x1,y1,x2,y2]]` **%** — bỏ box detect **overlap >50%** (`area(box∩zone)/area(box)`) với vùng. Áp **ngay sau `model.predict`, trước khi set person_detected/count/best_box** → vùng-chỉ-có-TV → `person_detected=False` → **không verify, không crop**. Giải false-positive **người hiển thị trên TV/màn hình** (cam này có dàn TV mép trái). Rule **overlap-fraction (không center-point)**: người thật chồng <50% vào vùng vẫn GIỮ.

**Files đụng:** `db.py` (cột `verify_crop` JSONB + `set_verify_crop`), `config.py` (`cameras_from_table` đọc `verify_crop`), `monitor.py` (`crop_person_with_padding`/`box_zone_overlap`/`ignore_zones_px` + filter trong `_monitor_loop`), `app.py` (`POST /api/camera/verify-crop/{name}`), `camera_detail.html` (block "Ảnh đưa AI nhận diện").

**Verify (đo thật, cùng 1 frame, imgsz 1280/conf 0.25 để TV bị detect):** người-trong-TV box overlap **100%** zone `[0,15,26,52]%` → BỎ; người thật giữa cửa overlap **0%** → GIỮ. Integration: TV-only frame → `person_detected=False`. HTTP endpoint 200, persist DB OK.

### 9.5 Config LIVE AI (cam "Cửa cty HCM", 2026-06-30)

- `vision_model = gc/gemini-2.5-flash` · `ai_base_url = http://host.docker.internal:20128/v1` · `ai_api_key = 9router-dummy`
- `cameras.prompt_id = person-id-entry` (prompt "Nhận diện người vào")
- `cameras.verify_crop = {enabled:true, padding:0.15, ignore_zones:[[0,15,26,52]]}` (vùng = dàn TV mép trái)

### 9.6 Quan trọng — bộ đôi vùng-loại-trừ × tuning detect

Global detect `imgsz=640/conf=0.5` **MISS người xa** (live `people=0` dù có người ở cửa) → verify/crop KHÔNG trigger. Vùng loại trừ TV cho phép **mạnh tay tăng `yolo_imgsz` (960-1280) + giảm `confidence` (~0.3)** để bắt người thật xa mà KHÔNG sợ TV bắn false-positive. ⚠️ Caveat hình học: người thật **đứng che trực tiếp trước TV** (>50% box trong vùng) có thể bị bỏ — chấp nhận với cam cửa.

### 9.7 Còn lại

- Nhập **Telegram token** → alert gửi thật.
- Cân nhắc **preview vẽ `ignore_zones`** lên frame (như `counting_preview` §6c) để đặt vùng trực quan thay gõ số.
- Tuning `yolo_imgsz`/`confidence` cho cam này (xem §9.6) — chưa làm.
