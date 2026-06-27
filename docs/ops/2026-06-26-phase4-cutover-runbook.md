# Phase 4 Cutover Runbook — camera-ai Deploy / Flip

**Ngày soạn:** 2026-06-26
**Trạng thái:** DRAFT — dùng làm guide cho session `dcnet-deploy`
**VM prod:** `163.227.121.206` (`ssh camera`), domain `camera-test.dcnet.vn`
**Spec nguồn:** `docs/superpowers/specs/2026-06-26-phase4-deploy-cutover-design.md`

---

## Mục lục

1. [Resolve-at-deploy checklist (O1–O9)](#resolve-at-deploy-checklist)
2. [Giai đoạn A — Chuẩn bị](#giai-đoạn-a--chuẩn-bị)
3. [Giai đoạn B — Parity verification (≥1 ngày làm việc)](#giai-đoạn-b--parity-verification)
4. [Giai đoạn C — Flip](#giai-đoạn-c--flip)
5. [Giai đoạn D — Decommission (≥1–3 ngày ổn định)](#giai-đoạn-d--decommission)
6. [Verification sau cutover](#verification-sau-cutover)
7. [Rollback](#rollback)
8. [Rủi ro & giảm thiểu](#rủi-ro--giảm-thiểu)
9. [Requirements x86 reconcile note](#requirements-x86-reconcile-note)

---

## Resolve-at-deploy checklist

Kiểm và giải quyết trên VM **trước khi** bắt đầu Giai đoạn A. Đánh dấu từng mục xong.

| # | Vấn đề | Lệnh kiểm trên VM | Tiêu chí pass |
|---|--------|-------------------|---------------|
| O1 | Thư mục clone camera-ai trên VM | `ls -d /opt/camera-ai 2>/dev/null \|\| echo MISSING` | `/opt/camera-ai` tồn tại hoặc xác nhận thư mục đích với team system trước khi clone |
| O2 | Tên compose project stack DCNET (ảnh hưởng prefix network nội bộ) | `docker compose ls` | Ghi nhận tên project (vd: `camera`, `dcnet`) → dùng khi thêm network `dcnet-shared` |
| O3 | VM đủ RAM/disk cho 2× postgres chạy song song | `free -h && df -h` | RAM còn trống ≥ 512 MB sau khi cả 2 stack live; disk `/var/lib/docker` còn ≥ 5 GB |
| O4 | RTSP port 554 reach từ VM IP đến cam NAT public | `nc -zv 115.79.47.96 554` | Kết nối thành công (Connection succeeded); nếu không → dùng fallback cam_proxy §2.7 |
| O5 | go2rtc image version pin | Chọn version tại `https://github.com/AlexxIT/go2rtc/releases`; mặc định đề xuất `alexxit/go2rtc:latest` | Pin version trong `docker-compose.prod.yml` trước build (tránh regression do `latest` thay đổi) |
| O6 | Backup cron postgres camera-ai | `crontab -l` trên VM | Nếu chưa có cron: thêm sau A5 (xem [§8.5 spec](2026-06-26-phase4-deploy-cutover-design.md)): `0 2 * * * docker exec camera-ai-postgres-1 pg_dump -U dcnet dcnet \| gzip > /opt/backup/dcnet_$(date +\%Y\%m\%d).sql.gz` |
| O7 | Broker migration (alternate) | N/A | **DEFER** hậu-cutover — mosquitto DCNET giữ chạy vô thời hạn (§2.1 spec) |
| O8 | Thêm user viewer ngoài admin sau cutover | FDW hiện **không có** route `/admin/users` (Phase 3 chưa implement) | Tạo user bằng INSERT trực tiếp vào postgres camera-ai: `docker exec -it camera-ai-postgres-1 psql -U dcnet dcnet -c "INSERT INTO users (email, password_hash, role) VALUES ('user@dcnet.vn', '<bcrypt_hash>', 'viewer');"` — tạo bcrypt hash với: `python3 -c "import bcrypt; print(bcrypt.hashpw(b'password', bcrypt.gensalt()).decode())"` |
| O9 | Auth gate cho `/live/*` và `/cam/*` | Kiểm FDW code: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8090/api/auth/check` sau khi stack camera-ai up | **ĐÃ GIẢI — Task 3:** forward_auth + `/api/auth/check` đã implement. `/live/*` trong Caddyfile post-flip dùng `forward_auth fall_detection_web:8090 { uri /api/auth/check }` (xem `Caddyfile.post-flip.draft`) |

---

## Giai đoạn A — Chuẩn bị

> **Mục tiêu:** Stack camera-ai chạy song song trên VM, chưa public — old stack DCNET vẫn là primary.

**A1. SSH vào VM:**
```bash
ssh camera   # alias → 163.227.121.206
```

**A2. Clone camera-ai vào `/opt/camera-ai`** (xác nhận với O1):
```bash
cd /opt
git clone git@github.com:dcnet/camera-ai.git camera-ai
cd /opt/camera-ai
git checkout main   # hoặc tag release khi có
```

**A3. Tạo `.env` từ `.env.example`:**
```bash
cp .env.example .env
# Điền secrets (KHÔNG commit):
#   DB_PASSWORD=<random ≥ 16 chars>
#   SECRET_KEY=<random ≥ 32 chars>
#   JWT_SECRET_KEY=<random ≥ 32 chars>
#   MQTT_PASSWORD=<cloud broker password>
#   MQTT_CLIENT_ID=event_collector_cameraai   ← PHẢI khác event_collector DCNET
#   CAM_USER=<axis cam username>
#   CAM_PASS=<axis cam password>
#   CAM_IP=115.79.47.96
#   CAM_RTSP_PORT=554
nano .env
```

**A4. Tạo external docker network `dcnet-shared`:**
```bash
docker network create dcnet-shared
# Verify:
docker network ls | grep dcnet-shared
```

**A5. Kết nối Caddy container DCNET vào `dcnet-shared`:**
```bash
# Lấy tên container Caddy (từ O2 — tên project ảnh hưởng):
docker compose ls   # xem tên project stack DCNET
# Thường là: camera-caddy-1 hoặc caddy_caddy_1
docker network connect dcnet-shared <caddy_container_name>
# Verify:
docker network inspect dcnet-shared | grep -A2 '"Name":'
```

**A6. Build + start camera-ai stack:**
```bash
cd /opt/camera-ai
docker compose -f docker-compose.prod.yml up -d --build
# Chờ healthy (~60s):
docker compose -f docker-compose.prod.yml ps
# Expected: postgres (healthy), fall_detection_web (Up), event_collector (Up), go2rtc (Up)
```

**A6-verify. Xác nhận fall_detection_web + go2rtc đã join `dcnet-shared`:**
```bash
docker network inspect dcnet-shared | grep -E '"Name"|fall_detection|go2rtc'
# Expect: cả fall_detection_web và go2rtc container xuất hiện trong output
# (chúng auto-join qua khai báo networks: dcnet-shared trong docker-compose.prod.yml)
# → DCNET Caddy có thể reach chúng qua network này
```

**A7. Đổi mật khẩu admin mặc định qua UI — PHẢI làm trước flip (bước C):**
- Truy cập staging route (thêm tạm vào Caddyfile nếu cần): `/staging/* → fall_detection_web:8090`
- Hoặc port-forward local: `ssh -L 9090:localhost:8090 camera`
- Login `admin` / `admin` → vào `/settings` → đổi password mạnh.
- ⚠️ Nếu chưa đổi → **KHÔNG được flip** (Caddy basic_auth bị bỏ sau C1, mật khẩu default lộ).

---

## Giai đoạn B — Parity verification

> **Mục tiêu:** Xác minh camera-ai đếm đúng so với DCNET trong cùng cửa sổ thời gian. **Giữ ≥ 1 ngày làm việc.**

**B1. Verify event_collector camera-ai nhận MQTT:**
```bash
docker compose -f docker-compose.prod.yml logs -f event_collector
# Mong đợi: INSERT log khi có người qua cửa
# Kiểm psql:
docker exec camera-ai-postgres-1 psql -U dcnet dcnet -c "SELECT count(*), direction FROM events GROUP BY direction;"
# Số tăng sau mỗi crossing = OK
```

**B2. Thêm staging route tạm vào Caddyfile DCNET** (để test FDW mà không public flip):
```caddy
# Thêm vào site block camera-test.dcnet.vn (TẠM THỜI — xóa sau khi B xong):
handle /staging/* {
    uri strip_prefix /staging
    reverse_proxy fall_detection_web:8090
}
```
```bash
# Reload Caddy:
docker exec <caddy_container> caddy reload --config /etc/caddy/Caddyfile
# Truy cập:
# https://camera-test.dcnet.vn/staging/ → login FDW, kiểm trang đếm
```

**B3. Parity check — queries so sánh:**

Chọn `T_start` = thời điểm event_collector_cameraai start (lấy từ log: dòng `connected to broker`).
Lý tưởng: bắt đầu check sau 00:00 VN+7 ngày tiếp theo để occupancy cũng so sánh được.

```sql
-- DCNET postgres (dcnet_camera DB):
docker exec <dcnet_postgres_container> psql -U dcnet dcnet_camera -c \
  "SELECT direction, COUNT(*) FROM events WHERE ts >= '<T_start>'::timestamptz GROUP BY direction;"

-- camera-ai postgres (dcnet DB):
docker exec camera-ai-postgres-1 psql -U dcnet dcnet -c \
  "SELECT direction, COUNT(*) FROM events WHERE ts >= '<T_start>'::timestamptz GROUP BY direction;"
```

Kiểm theo giờ (phát hiện gap):
```sql
-- Chạy trên cả 2, so sánh từng bucket giờ:
SELECT date_trunc('hour', ts AT TIME ZONE 'Asia/Ho_Chi_Minh') AS hour_vn,
       direction, COUNT(*)
FROM events
WHERE ts >= '<T_start>'::timestamptz
GROUP BY 1, 2
ORDER BY 1, 2;
```

**B4. Pass criteria:**
- Delta IN ≤ 2 event trong cửa sổ ≥ 4h → **PASS**
- Không có "event gap" (giờ nào camera-ai = 0 mà DCNET > 0) → **PASS**
- Occupancy: chỉ compare nếu event_collector_cameraai đã live **trước 00:00 VN+7 của ngày đo**. Nếu start giữa ngày: chỉ so IN/OUT count trong cửa sổ chung, skip occupancy.
- Systematic drift (camera-ai thiếu event liên tục) → **FAIL** → debug collector trước khi flip.

**B5. Debug nếu fail:**
```bash
# Kiểm reconnect loop:
docker compose -f docker-compose.prod.yml logs event_collector | grep -E 'reconnect|error|UNIQUE'
# Kiểm TZ container:
docker exec camera-ai-postgres-1 date
# Kiểm UNIQUE constraint (bình thường — ON CONFLICT DO NOTHING):
docker compose -f docker-compose.prod.yml logs event_collector | grep 'conflict'
```

---

## Giai đoạn C — Flip

> **Điều kiện tiên quyết:**
> - [ ] B4 PASS (parity OK)
> - [ ] A7 DONE (admin password đổi)
> - [ ] O9 RESOLVED (forward_auth /api/auth/check verified)
> - [ ] Backup Caddyfile cũ

**C0. Backup Caddyfile hiện tại:**
```bash
# Phương pháp khuyến nghị (Dockerized DCNET Caddy — /etc/caddy không mount ra host):
docker exec <caddy_container> cat /etc/caddy/Caddyfile > /opt/camera/Caddyfile.pre-flip-$(date +%Y%m%d-%H%M)

# Phương pháp cp trực tiếp (chỉ dùng nếu Caddy mount /etc/caddy ra host):
# cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.pre-flip-$(date +%Y%m%d-%H%M)
```

**C1. Sửa Caddyfile — thay site block `camera-test.dcnet.vn`:**

Xóa toàn bộ nội dung site block cũ (có `basic_auth` + upstream `dashboard:8501`).
Thay bằng nội dung từ `docs/ops/Caddyfile.post-flip.draft` (verbatim):

```caddy
camera-test.dcnet.vn {
    encode zstd gzip

    # Live view (go2rtc WS) — gate qua FDW JWT trước khi forward
    handle /live/* {
        forward_auth fall_detection_web:8090 {
            uri /api/auth/check
            copy_headers Cookie
        }
        reverse_proxy go2rtc:1984 {
            header_up Upgrade {http.request.header.Upgrade}
            header_up Connection {http.request.header.Connection}
        }
    }

    # Cam snapshot/mjpeg fallback (chỉ nếu go2rtc chưa verified — §2.7)
    # handle /cam/* {
    #     forward_auth fall_detection_web:8090 { uri /api/auth/check; copy_headers Cookie }
    #     reverse_proxy cam_proxy:80
    # }

    handle {
        reverse_proxy fall_detection_web:8090
    }
}
# Mosquitto cert-sync logic KHÔNG đổi (broker 8883 mượn cert Caddy).
```

> ⚠️ **Bỏ `/staging/*` route tạm** đã thêm ở B2.

**C1.5. Validate config trước khi reload (good practice):**
```bash
docker exec <caddy_container> caddy validate --config /etc/caddy/Caddyfile
# Expect: "Valid configuration" — chỉ tiếp tục nếu validate PASS
# caddy reload là atomic/fail-safe nhưng validate-first giúp bắt lỗi syntax sớm
```

**C2. Reload Caddy (zero-downtime):**
```bash
docker exec <caddy_container> caddy reload --config /etc/caddy/Caddyfile
# Verify không lỗi:
docker logs <caddy_container> --tail=20
```

**C3. Smoke test sau flip:**
```bash
# 1. Login form FDW (không phải Streamlit):
curl -sI https://camera-test.dcnet.vn/ | head -5
# Expect: 200 hoặc redirect đến /login

# 2. Auth gate (quan trọng — xem Verification #6):
curl -s -o /dev/null -w "%{http_code}" https://camera-test.dcnet.vn/api/counting
# Expect: 401 (KHÔNG phải 200)

# 3. Mosquitto cert vẫn OK:
openssl s_client -connect camera-test.dcnet.vn:8883 -showcerts </dev/null 2>&1 | grep -E 'Verify|subject'
```

Truy cập browser `https://camera-test.dcnet.vn/`:
- Login bằng admin (mật khẩu đã đổi ở A7) → trang đếm hiển thị
- Xác nhận occupancy + IN/OUT hiển thị
- Xác nhận live view load (go2rtc websocket)

---

## Giai đoạn D — Decommission

> **Điều kiện:** flip đã ổn định ≥ 1–3 ngày; smoke test + counting đều OK.
> ⚠️ **Thứ tự tắt nghiêm ngặt — KHÔNG `docker compose down` toàn stack DCNET** (sẽ tắt mosquitto).

**D1. Dừng dashboard DCNET:**
```bash
docker stop camera-dashboard-1   # tên container từ O2
# Verify trang vẫn hoạt động (Caddy đang route đến FDW, không phải dashboard):
curl -sI https://camera-test.dcnet.vn/ | head -3
```

**D2. Dừng event_collector DCNET:**
```bash
docker stop camera-event_collector-1
# Mosquitto vẫn chạy (cam Axis vẫn publish; camera-ai collector vẫn subscribe)
# Verify camera-ai collector vẫn nhận events:
docker compose -f /opt/camera-ai/docker-compose.prod.yml logs -f event_collector --tail=20
```

**D3. `/cam/*` route trong Caddyfile — xử lý theo kết quả O4:**

> **Nhánh A — O4 PASS (go2rtc hoạt động, live view qua `/live/*` OK):**
> Route `/cam/*` đang ĐƯỢC COMMENT OUT trong `Caddyfile.post-flip.draft` → **giữ nguyên trạng thái commented out (inactive)**. Operator có thể xóa hẳn commented block để dọn file, nhưng **KHÔNG được uncomment** — uncomment sẽ kích hoạt một route trỏ vào cam_proxy mà D4 sẽ tắt ngay sau đó.
> ```bash
> # Không cần sửa Caddyfile trong nhánh này.
> # (Tùy chọn) Xóa block comment /cam/* để dọn file:
> # nano /etc/caddy/Caddyfile   # xóa 4 dòng comment /cam/* block
> # → validate + reload nếu có sửa:
> # docker exec <caddy_container> caddy validate --config /etc/caddy/Caddyfile
> # docker exec <caddy_container> caddy reload --config /etc/caddy/Caddyfile
> ```
> Tiếp tục D4 (dừng cam_proxy).

> **Nhánh B — O4 FAIL (go2rtc không reach được RTSP 554, live view offline):**
> KHÔNG tắt cam_proxy. Thay vào đó: uncomment `/cam/*` trong Caddyfile để dùng cam_proxy làm fallback live view, sau đó validate + reload. **Bỏ qua D4** (cam_proxy phải giữ chạy).
> ```bash
> # Uncomment /cam/* block trong Caddyfile (xóa dấu # ở 4 dòng handle /cam/* block)
> docker exec <caddy_container> caddy validate --config /etc/caddy/Caddyfile
> docker exec <caddy_container> caddy reload --config /etc/caddy/Caddyfile
> # → SKIP D4
> ```

**D4. Dừng cam_proxy DCNET** (chỉ khi go2rtc đã verified — O4 PASS, Nhánh A ở D3):
```bash
docker stop camera-cam_proxy-1
```

**D5. Postgres DCNET — KHÔNG xóa ngay:**
```bash
# Giữ volume postgres_data DCNET tối thiểu 7 ngày cho audit/rollback:
# Ghi ngày dừng:
echo "$(date): postgres DCNET vẫn chạy, giữ đến $(date -d '+7 days' '+%Y-%m-%d')" >> /opt/camera/decommission.log
# Sau 7 ngày:
# docker stop camera-postgres-1
# docker volume rm camera_postgres_data   # chỉ sau khi chắc chắn không cần
```

**D6. Mosquitto DCNET — KHÔNG tắt:**
```bash
# Mosquitto là shared infra — cam Axis vẫn publish vào đây.
# Chỉ tắt khi có kế hoạch broker migration riêng (defer hậu-cutover — O7).
```

---

## Verification sau cutover

Chạy toàn bộ 8 check sau khi flip (giai đoạn C3 + bổ sung):

**#1 Login form FDW:**
```bash
curl -sI https://camera-test.dcnet.vn/ | grep -E 'HTTP|Location'
# Expect: login form FDW (Jinja template), KHÔNG phải Streamlit
```

**#2 Đếm cập nhật realtime:**
- Browser: login → trang đếm → xác nhận occupancy + IN/OUT hôm nay
- Chờ 1 crossing thật → số tăng trong ≤ 5s (nếu có auto-refresh)

**#3 Live view go2rtc:**
- Truy cập trang Camera/Live view trong FDW
- Stream load qua WebSocket `/live/*`
- Nếu fail → kiểm O4 (RTSP 554 unreachable); fallback: uncomment `/cam/*` route

**#4 Collector logs:**
```bash
docker compose -f /opt/camera-ai/docker-compose.prod.yml logs event_collector --tail=50
# Expect: INSERT log đều, không reconnect loop
```

**#5 All services healthy:**
```bash
docker compose -f /opt/camera-ai/docker-compose.prod.yml ps
# Expect: tất cả Up (healthy)
```

**#6 Auth gate — CRITICAL:**
```bash
# Unauthenticated → phải 401 hoặc redirect login (KHÔNG phải 200):
curl -s -o /dev/null -w "%{http_code}" https://camera-test.dcnet.vn/api/counting
# → phải là 401

curl -s -o /dev/null -w "%{http_code}" https://camera-test.dcnet.vn/api/events
# → phải là 401

curl -s -o /dev/null -w "%{http_code}" https://camera-test.dcnet.vn/live/
# → phải là 401 hoặc 302/redirect (KHÔNG 200)

# Bất kỳ route nào trả 200 mà không có credentials = security regression → BLOCK decommission
```

**#7 Mosquitto TLS cert:**
```bash
openssl s_client -connect camera-test.dcnet.vn:8883 -showcerts </dev/null 2>&1 | grep -E 'Verify return code|subject='
# Expect: "Verify return code: 0 (ok)"
# Cam vẫn publish: kiểm event row tăng sau crossing thật
```

**#8 Caddy cert renew không bị ảnh hưởng:**
```bash
docker exec <caddy_container> caddy validate --config /etc/caddy/Caddyfile
# Expect: valid config, no errors
# cert logic mosquitto (cert-sync.sh) không thay đổi → cert path vẫn thuộc stack DCNET Caddy
```

---

## Rollback

> **Rollback nhanh — old stack không bị tắt trong giai đoạn song song:**

**R1. Flip Caddy ngược lại:**
```bash
# Restore Caddyfile pre-flip backup:
cp /opt/camera/Caddyfile.pre-flip-backup /etc/caddy/Caddyfile
# Hoặc sửa thủ công: thay upstream fall_detection_web:8090 → dashboard:8501,
# restore basic_auth block (cần hash bcrypt từ backup)
docker exec <caddy_container> caddy reload --config /etc/caddy/Caddyfile
```

**R2. Dashboard DCNET vẫn chạy → live ngay:**
```bash
# Verify:
curl -sI https://camera-test.dcnet.vn/ | head -3
# Expect: Streamlit dashboard trả về HTML
```

**R3. Nếu đã decommission dashboard (giai đoạn D):**
```bash
docker start camera-dashboard-1   # container vẫn tồn tại (chỉ stop, không xóa)
# Postgres DCNET vẫn còn data (volume chưa xóa trong ≤7 ngày post-D)
```

**Cửa sổ rollback an toàn:** Tối thiểu 7 ngày sau D1 (stop dashboard DCNET). Sau khi xóa volume postgres DCNET → rollback không còn data lịch sử.

**Không thể rollback bằng cách này nếu:** đã rebind broker Mosquitto sang camera-ai compose (alternate §2.1) — đây là lý do giữ mosquitto stack DCNET là shared infra không tắt.

---

## Rủi ro & giảm thiểu

| Rủi ro | Mức | Giảm thiểu |
|--------|-----|-----------|
| **Mosquitto tắt nhầm** khi dọn stack DCNET | **CAO** | Stop **selective** từng container (`docker stop dashboard`, `docker stop event_collector`). **KHÔNG** `docker compose down` toàn stack DCNET — lệnh đó tắt cả mosquitto. Mosquitto tiếp tục chạy vô thời hạn. |
| **Auth leak** — `/cam/*` hoặc `/live/*` lộ unauthenticated sau bỏ basic_auth | **CAO** | O9 đã giải (forward_auth + /api/auth/check). Verify #6 bắt buộc trước khi chạy D. Bất kỳ route nào trả 200 không cred = block decommission, rollback ngay. |
| **Schema race** — event_collector INSERT trước FDW init_db | **CAO** | `db/init.sql` mount vào postgres (Task 1) — schema tạo trong postgres init trước mọi service start. `init_db()` FDW đã `CREATE TABLE IF NOT EXISTS` (idempotent). |
| Admin password chưa đổi trước flip | Cao | A7 là bước bắt buộc, gate flip. Checklist C không proceed nếu A7 chưa done. |
| RTSP 554 không reach → live view offline | Trung | Go2rtc log ngay khi start; kiểm O4. Fallback: uncomment `/cam/*` route trong Caddyfile (cam_proxy:80). Live view failure không ảnh hưởng counting — flip vẫn proceed. |
| Parity fail do message loss | Trung | Parallel window ≥ 1 ngày làm việc; debug collector log trước flip; rollback đơn giản (Caddy flip ngược). |
| go2rtc WebSocket Caddy timeout | Thấp | Header `Upgrade`/`Connection` đã set trong Caddyfile draft. Nếu vẫn timeout: thêm `transport { dial_timeout 30s }` vào reverse_proxy block. |
| 2× postgres RAM pressure | Thấp | Counting DB nhỏ (event rows only). Verify O3 (`free -h`) trước A5. Postgres mặc định ~50-100 MB RAM mỗi instance. |

---

## Requirements x86 reconcile note

**Không thay đổi Dockerfile bây giờ — quyết định tại deploy.**

Tình huống:
- **Dev (arm64 Mac):** `requirements.docker.txt` — plain `torch` (PyPI default; build ra ARM wheels hoặc CPU).
- **Prod VM (x86_64 linux):** `requirements.txt` + `--extra-index-url https://download.pytorch.org/whl/cpu` sẽ cho CPU-only wheels (nhỏ hơn ~2 GB so với CUDA build). Image nhỏ hơn, build nhanh hơn, không cần GPU.

Hành động khi deploy trên VM x86:
```bash
# Option A: override build arg (nếu Dockerfile hỗ trợ ARG REQUIREMENTS_FILE):
docker compose -f docker-compose.prod.yml build \
  --build-arg REQUIREMENTS_FILE=requirements.txt

# Option B: đổi COPY trong Dockerfile.prod (tạo riêng cho prod):
# COPY requirements.txt .
# RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# Verify build thành công trên x86 trước flip; không verify được trên arm64 dev.
```

Tốc độ ưu tiên tại deploy: nếu image đã build OK với `requirements.docker.txt` trên x86 và size chấp nhận được → giữ nguyên. Chỉ switch nếu CUDA wheels gây vấn đề (disk space, build time).

---

## Liên quan

- Spec: [`docs/superpowers/specs/2026-06-26-phase4-deploy-cutover-design.md`](../superpowers/specs/2026-06-26-phase4-deploy-cutover-design.md)
- Caddyfile draft: [`docs/ops/Caddyfile.post-flip.draft`](Caddyfile.post-flip.draft)
- Migration tổng thể: [`docs/superpowers/specs/2026-06-26-dcnet-platform-migration-design.md`](../superpowers/specs/2026-06-26-dcnet-platform-migration-design.md)
- Deploy skill: `dcnet-deploy`
