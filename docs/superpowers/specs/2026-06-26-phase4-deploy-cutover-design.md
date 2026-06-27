# Design — Phase 4: Deploy / Cutover

**Ngày:** 2026-06-26
**Trạng thái:** PREP DONE (artifacts + runbook — plan `../plans/2026-06-26-phase4-deploy-cutover.md`, branch `feat/phase4-deploy-prep`; VM cutover pending dcnet-deploy session)
**Phase:** 4 / 5 (xem tổng thể: `2026-06-26-dcnet-platform-migration-design.md`)
**Tiền đề:** Phase 0–3 DONE + merged — camera-ai chạy Postgres (incidents/users/settings/cameras/events), `event_collector` counting, UI đếm/live/group đủ parity Streamlit, modular per-camera toggles.
**Nguồn prod hiện tại:** repo `dcnet-cloud/camera`, `docker-compose.prod.yml`, `docs/ops/camera-state.yml`.

---

## 1. Mục tiêu

**camera-ai THAY THẾ Streamlit dashboard trên VM prod** `camera-test.dcnet.vn` (163.227.121.206) mà **không gãy đếm live** trong quá trình chuyển.

Cụ thể:
1. Dựng `docker-compose.prod.yml` cho camera-ai (Postgres + fall_detection_web + event_collector + go2rtc + Caddy tích hợp; `reid_worker` off).
2. Chạy camera-ai **song song** với stack DCNET cũ trên cùng VM (parallel read-ké cloud broker — đã thiết kế ở Phase 1).
3. Xác minh **parity** (số IN/OUT/occupancy khớp trong 1 cửa sổ thời gian).
4. Flip reverse-proxy (thay upstream Caddy) → camera-ai trở thành service chính.
5. Decommission dashboard cũ + event_collector cũ (giữ lại mosquitto và postgres cũ tạm thời).

**Không thuộc scope Phase 4:** LLM NL query; bật lại Re-ID inference (cần đặt lại cam); thay đổi DNS (A-record → .206 giữ nguyên).

---

## 2. Quyết định đã chốt

### 2.1 Broker: SHARED INFRA — mosquitto prod không thuộc về camera-ai

Mosquitto container trên VM (`camera-test.dcnet.vn:8883`) là broker mà **cam Axis** đang publish vào (single-broker target, configured qua VAPIX). Đây là infra dùng chung — **KHÔNG tắt khi cutover**.

Chiến lược:
- `docker-compose.prod.yml` của camera-ai **OMIT mosquitto** — không khai báo service mosquitto.
- `event_collector_cameraai` nối cloud broker TLS 8883 với `MQTT_CLIENT_ID=event_collector_cameraai` (distinct client-id, đọc ké như Mac dev đang làm — xem `docs/ops/camera-state.yml: collectors`).
- Sau khi decommission stack cũ: mosquitto container của stack cũ **vẫn chạy**, chỉ `docker stop` dashboard + event_collector DCNET.

**Alternate (flag Open):** camera-ai nhận quyền sở hữu broker (mosquitto container di chuyển sang camera-ai compose, tắt broker cũ trong khoảng thời gian `cleanSession=false` buffer của cam, cam rebind 8883 = cùng host nên không đổi địa chỉ). Ưu: kiến trúc sạch hơn sau này. Nhược: rủi ro trong cutover (cần stop/start broker + confirm cam reconnect), vi phạm nguyên tắc "không gãy đếm". → Defer sang audit hậu-cutover.

### 2.2 Caddy: Extend, không replace

VM chỉ có 1 process bind 80/443; Caddyfile hiện tại cũng cấp cert LE mà mosquitto mượn cho 8883 TLS (via `scripts/deploy/cert-sync.sh`). Không spin up Caddy thứ 2.

Flip = **sửa 1 upstream** trong Caddyfile hiện tại: `dashboard:8501` → `fall_detection_web:8090` (hostname qua shared docker network). Không đổi DNS.

### 2.3 Cross-compose networking

Old stack (compose project `camera`) và new stack (compose project `camera-ai`) cần Caddy reach `fall_detection_web`. Cách:
- Tạo external docker network `dcnet-shared` → cả 2 compose project tham gia.
- Caddy container (thuộc stack cũ) join `dcnet-shared` → reach `fall_detection_web` của stack mới theo hostname.
- **Open (confirm khi review):** VM đang chạy tên compose project là gì (`camera` hay default dir name)? Ảnh hưởng tên network nội bộ.

### 2.4 Auth model: Caddy basic_auth OFF tại flip

Old stack: Caddy `basic_auth` → `X-Auth-User` header → Streamlit role gate (viewer/admin).
New stack: FDW JWT login form — auth do app tự xử lý.

**Khi flip upstream sang fall_detection_web: phải bỏ `basic_auth` block trong Caddyfile** (không thì user double-login). Đồng thời, mật khẩu admin mặc định (`admin/admin`) phải đổi ngay qua UI trước khi Caddy mở route (task đầu tiên trong checklist cutover ops).

### 2.5 Parity definition: incremental forward, không total historical

camera-ai dùng greenfield DB + client-id mới → **không nhận backlog** (broker MQTT chỉ queue cho persistent session đã có sẵn; client-id mới bắt đầu từ thời điểm connect). Do đó:

**Parity = so sánh IN/OUT/occupancy trong cửa sổ chung** (từ lúc cả 2 collector đang chạy song song, lý tưởng nhất = bắt đầu từ 00:00 VN+7 ngày hôm sau). Không expect tổng lịch sử khớp.

### 2.6 reid_worker: off mặc định qua Docker profile

`reid_worker` khai báo trong compose nhưng gated bằng `profiles: [reid]` — không start trừ khi explicit `--profile reid`. Giữ code + service definition sẵn sàng để bật khi đặt lại cam.

### 2.7 go2rtc live view: primary path + fallback cam_proxy

go2rtc service (RTSP → HLS/WebRTC) internal-only trong compose network. Expose qua Caddy route `/live/*` (websocket forward). RTSP từ cam đến go2rtc qua `115.79.47.96:554` (cam NAT public, VM reach được — đã verify VAPIX reach; **port 554 chưa verify từ VM** — xem Open O4). **Không thêm port ufw mới** — go2rtc không publish host port.

**Fallback nếu RTSP 554 không reach:** giữ `cam_proxy` nginx (stack DCNET) chạy và giữ route `/cam/*` trong Caddyfile — FDW đọc snapshot/mjpeg qua proxy URL nội bộ (pattern như Streamlit đã làm). cam_proxy KHÔNG decommission cho đến khi go2rtc verified. Đây là **nhánh fallback first-class**, không chỉ là footnote — bước D4 (§5.D) gate rõ ràng trên việc go2rtc có verified hay không.

*Ghi chú vs migration spec tổng thể (§3 topology): migration design liệt kê cả `cam_proxy` + `go2rtc` trong topology đích — Phase 4 chọn go2rtc làm primary và giữ cam_proxy làm fallback, đây là refinement có chủ ý (không contradicts).*

---

## 3. Topology prod đích

```
Cam Axis (ACAP OA) ─MQTT/TLS 8883─► mosquitto (VM, stack DCNET, shared infra — KHÔNG tắt)
                                          │
                    ┌─────────────────────┴────────────────────────────────────┐
                    ▼ MQTT_CLIENT_ID=event_collector               ▼ MQTT_CLIENT_ID=event_collector_cameraai
             event_collector (stack DCNET)                   event_collector (stack camera-ai)
                    │                                               │
                    ▼                                               ▼
             postgres DCNET                                  postgres camera-ai
             (dcnet_camera DB)                               (dcnet DB — greenfield)
                    │                                               │
                    ▼                                               ▼
             dashboard Streamlit :8501              fall_detection_web FastAPI/Jinja :8090
                    │                                               │
                    └───────────────────┬───────────────────────────┘
                                        ▼
                                   Caddy :80/:443
                          (camera-test.dcnet.vn TLS LE)
                          [PRE-FLIP → upstream: dashboard:8501]
                          [POST-FLIP → upstream: fall_detection_web:8090]
                                        │
                                   /cam/* → cam_proxy:80
                                   /live/* → go2rtc:1984 (ws)
```

**Stack DCNET cũ (stack song song):** postgres + mosquitto + event_collector + cam_proxy + dashboard + caddy.
**Stack camera-ai mới:** postgres + fall_detection_web + event_collector + go2rtc + (reid_worker off) — NO mosquitto, NO cam_proxy riêng (go2rtc reach cam trực tiếp RTSP).

---

## 4. `docker-compose.prod.yml` camera-ai (design-level)

Đây là thiết kế service-level cho file sẽ tạo ở `camera-ai/docker-compose.prod.yml`. Không phải file thực — plan-level.

### 4.1 Diffs vs `docker-compose.yml` (dev)

| Dimension | Dev (`docker-compose.yml`) | Prod (`docker-compose.prod.yml`) |
|---|---|---|
| postgres host port | `5432:5432` (expose) | **Không publish** — internal only |
| fall_detection_web host port | `8090:8090` | **Không publish** — chỉ qua Caddy |
| go2rtc | Không có | Thêm service, internal-only |
| event_collector | Không có (Phase 1 thêm) | Thêm service |
| reid_worker | Không có | Thêm service, `profiles: [reid]` (off) |
| cam_proxy nginx | Không có | Không có (go2rtc thay) |
| Caddy | Không có | **Không có** — tái dùng Caddy stack DCNET (shared) |
| `restart: unless-stopped` | Không (dev) | Có trên tất cả service |
| `env_file` | Relative path FDW | `.env` ở root camera-ai |
| Volumes | `pgdata`, `fdw_data` | `pgdata`, `fdw_data`, `go2rtc_data` |
| External network | Không có | Join `dcnet-shared` (external) để Caddy DCNET reach |

### 4.2 Services cần thiết

**`postgres`** — `pgvector/pgvector:pg16`. Volume `pgdata`. Init SQL mount cho schema Phase 0–3 (`./db/init.sql:/docker-entrypoint-initdb.d/01_init.sql:ro`) — **bắt buộc** để schema tồn tại trước khi bất kỳ service nào connect. Healthcheck `pg_isready`. Không publish port.

> **Schema bootstrap race:** DCNET prod mount init SQL → schema tạo ngay trong postgres init (trước app start). camera-ai hiện tạo schema qua `init_db()` trong FDW app. Nếu chỉ dùng `init_db()`, `event_collector` (which `depends_on postgres healthy`, NOT FDW) có thể `INSERT INTO events` trước khi FDW chạy `init_db()` → race condition lỗi. **Giải pháp: thêm `db/init.sql` (dump schema Phase 0–3) vào camera-ai, mount vào postgres** — đây là việc cần làm khi soạn plan Phase 4 (§13 bước 1). `init_db()` trong FDW vẫn giữ nhưng đổi thành idempotent `CREATE TABLE IF NOT EXISTS` (already safe) hoặc no-op khi bảng đã tồn tại.

**`fall_detection_web`** — build `./fall_detection_web`. Env: `DATABASE_URL`, `SECRET_KEY`, `JWT_*`. Volume `fdw_data:/app/data`. `depends_on: postgres (healthy)`. Không publish port ngoài (Caddy reach qua shared network hoặc nội bộ compose). Port nội bộ `8090`.

**`event_collector`** — build `./services/event_collector`. Env: `MQTT_HOST=camera-test.dcnet.vn`, `MQTT_PORT=8883`, `MQTT_TLS=true`, `MQTT_CLIENT_ID=event_collector_cameraai`, `DATABASE_URL`. `depends_on: postgres (healthy)`. Không publish port.

**`go2rtc`** — image `alexxit/go2rtc` (hoặc build riêng). Config `go2rtc.yaml` mount (khai báo RTSP stream cam: `rtsp://user:pass@115.79.47.96:554/axis-media/media.amp`). Expose port nội bộ `1984` (HTTP API + WebSocket). Không publish host port. **Open: go2rtc image version + config format (confirm khi review).**

**`reid_worker`** — build `./services/reid_worker`. `profiles: [reid]`. Không start mặc định.

### 4.3 Volumes

```
pgdata         → /var/lib/postgresql/data
fdw_data       → /app/data  (event images, teldrive_cache)
go2rtc_data    → /config    (go2rtc config)
```

### 4.4 Networks

```yaml
networks:
  default:
    name: camera-ai_default
  dcnet-shared:
    external: true          # pre-create: docker network create dcnet-shared
```

Caddy container của stack DCNET cũ cũng join `dcnet-shared` (thêm vào `docker-compose.prod.yml` DCNET hoặc `docker network connect`).

---

## 5. Cutover procedure (plan-level — không execute ở đây)

### Giai đoạn A — Chuẩn bị (pre-cutover)

**A1.** Clone camera-ai lên VM vào `/opt/camera-ai` (hoặc thư mục tương đương — **Open: confirm thư mục đích**).

**A2.** Tạo `.env` cho camera-ai từ `.env.example`: điền `DB_PASSWORD`, `SECRET_KEY`, `JWT_SECRET_KEY`, `MQTT_PASSWORD` (broker cred), `CAM_USER`/`CAM_PASS` (cho go2rtc RTSP URL), `MQTT_CLIENT_ID=event_collector_cameraai`. **Không commit secrets.**

**A3.** Tạo external docker network: `docker network create dcnet-shared`.

**A4.** Thêm `dcnet-shared` vào Caddy container của stack DCNET (network connect hoặc sửa compose DCNET). Verify Caddy có thể reach hostname `fall_detection_web` sau khi stack camera-ai lên.

**A5.** Build + start camera-ai stack (không có `-f docker-compose.prod.yml` nếu dev; prod dùng overlay):
```
docker compose -f docker-compose.prod.yml up -d --build
```
Mục tiêu: postgres, fall_detection_web, event_collector, go2rtc — tất cả healthy.

**A6.** Đổi mật khẩu admin mặc định qua UI FDW (`/settings`) **trước khi** mở Caddy route. Đây là bước bắt buộc trước khi flip.

### Giai đoạn B — Parallel verification (parity window)

**B1.** Verify event_collector camera-ai nhận MQTT: `docker compose logs event_collector` → thấy INSERT log cho crossing events (chờ lượt qua cửa thật hoặc kiểm psql `SELECT count(*) FROM events` tăng).

**B2.** Thêm route tạm trong Caddyfile (hoặc port-forward local) để access FDW mà không flip public. Vd: Caddy thêm path `/staging/*` → `fall_detection_web:8090`. Truy cập `https://camera-test.dcnet.vn/staging/` → verify:
- Login JWT hoạt động.
- Trang đếm hiển thị IN/OUT/occupancy (so khớp Streamlit dashboard).
- Live view (go2rtc websocket) load stream.
- `/api/counting` JSON trả đúng.

**B3.** Parity check (cửa sổ thời gian chung):
- Chọn cửa sổ T = [t_start, t_now] trong đó cả 2 collector đang live (từ khi event_collector_cameraai start, lý tưởng = 00:00 hôm sau).
- So sánh: `COUNT(IN WHERE ts >= t_start) DCNET postgres` == `COUNT(IN WHERE ts >= t_start) camera-ai postgres`. Tương tự OUT và occupancy.
- Chấp nhận sai số nhỏ do clock + message ordering; **không chấp nhận systematic drift** (nếu camera-ai thiếu event liên tục → debug collector trước khi flip).

**B4.** Giữ parallel window tối thiểu **1 ngày làm việc** (để có đủ crossing thật).

### Giai đoạn C — Flip

**C1.** Sửa Caddyfile — **bỏ `basic_auth` SITE-WIDE** và thay bằng cấu trúc route mới:

- **`/` (main handle):** upstream `fall_detection_web:8090`. Auth do FDW JWT xử lý — **KHÔNG còn `basic_auth`** ở đây.
- **`/live/*`:** upstream go2rtc:1984 (websocket). **KHÔNG có bare reverse_proxy không auth** — FDW JWT phải gate endpoint `/live/*` tại app layer (session cookie hoặc token check), hoặc Caddy `forward_auth` đến FDW session endpoint. Chọn cơ chế → **Open O9**.
- **`/cam/*`:** giữ route → `cam_proxy:80` **nhưng phải có auth gate**. Vì basic_auth site-wide đã bỏ, `/cam/*` không còn được bảo vệ tự động. Giải pháp: (a) route `/cam/*` qua FDW (FDW làm proxy nội bộ, tự JWT-gate) — **đề xuất**, hoặc (b) `forward_auth` Caddy → FDW `/api/auth/check`. Nếu chưa implement: **tắt `/cam/*` route tại flip** (cam_proxy vẫn chạy nhưng không expose) cho đến khi auth gate sẵn sàng.

> **⚠️ Security invariant:** sau khi bỏ Caddy basic_auth, **mọi route** phải được bảo vệ bởi FDW JWT (hoặc forward_auth). Không có bare `reverse_proxy` không auth. Vi phạm = lộ feed cam và data counting công khai. Verify ở §12 step 6.

```caddyfile
# POST-FLIP block (thay thế toàn bộ nội dung site block hiện tại)
camera-test.dcnet.vn {
    encode zstd gzip
    # basic_auth REMOVED — FDW JWT tự gate

    handle /live/* {
        # forward_auth fall_detection_web:8090 /api/auth/check   # TBD — Open O9
        reverse_proxy go2rtc:1984 {
            header_up Upgrade {http.request.header.Upgrade}
            header_up Connection {http.request.header.Connection}
        }
    }

    # /cam/* — route qua FDW hoặc tắt nếu chưa có auth gate (xem §2.7)
    # handle_path /cam/* { reverse_proxy fall_detection_web:8090 }   # TBD

    handle {
        reverse_proxy fall_detection_web:8090
    }
}
```

**C2.** `caddy reload` (hoặc `docker exec caddy caddy reload --config /etc/caddy/Caddyfile`). Zero-downtime reload — Caddy không restart, cert không ảnh hưởng.

**C3.** Smoke test sau flip:
- `https://camera-test.dcnet.vn/` → login form FDW (không phải Streamlit).
- Login admin (mật khẩu đã đổi ở A6) → trang đếm hiển thị.
- Xác nhận mosquitto cert-sync vẫn OK (broker 8883 không bị ảnh hưởng — Caddyfile không đổi cert logic).

### Giai đoạn D — Decommission (sau khi flip ổn định ≥ 1–3 ngày)

**Thứ tự tắt:**
1. `docker stop dashboard` (stack DCNET) → dừng Streamlit.
2. `docker stop event_collector` (stack DCNET) → dừng collector cũ (mosquitto vẫn chạy).
3. Xóa `/cam/*` route trong Caddyfile nếu camera-ai đã có go2rtc live view thay thế cam_proxy.
4. `docker stop cam_proxy` (stack DCNET, optional — chỉ khi go2rtc đã cover live view).
5. Postgres cũ: **KHÔNG xóa ngay** — giữ volume `postgres_data` (stack DCNET) tối thiểu 7 ngày để audit hoặc rollback nếu cần. Sau đó `docker stop postgres` (stack DCNET) → optionally `docker volume rm`.
6. **Mosquitto: KHÔNG tắt** — giữ chạy vô thời hạn cho đến khi có kế hoạch broker migration riêng.

---

## 6. Parity verification

### 6.1 Cửa sổ đo

- **T_start** = thời điểm event_collector_cameraai start + connected (lấy từ log: `connected to broker`).
- **T_end** = thời điểm check (now).
- Lý tưởng: bắt đầu parity check sau 00:00 ngày tiếp theo để cả 2 DB đều tính từ đầu ngày → occupancy so sánh trực tiếp.

### 6.2 Queries so sánh

Chạy song song trên 2 psql (DCNET postgres + camera-ai postgres):

```sql
-- DCNET (dcnet_camera DB):
SELECT direction, COUNT(*) FROM events
WHERE ts >= '<T_start>'::timestamptz GROUP BY direction;

-- camera-ai (dcnet DB):
SELECT direction, COUNT(*) FROM events
WHERE ts >= '<T_start>'::timestamptz GROUP BY direction;
```

Dashboard UI: so sánh số IN/OUT/occupancy trên Streamlit (cũ) vs `/api/counting` FDW (mới) cùng lúc.

### 6.3 Pass criteria

- Delta IN ≤ 2 event trong cửa sổ ≥ 4h (sai số do message ordering, không do systematic drop).
- **Occupancy:** chỉ compare nếu camera-ai đã live **kể từ trước 00:00 VN+7 của ngày đo** (collector start trước midnight) — điều kiện tiên quyết, không pass nếu chưa đủ điều kiện này (mid-day start luôn lệch vì thiếu event sáng sớm). Nếu collector start giữa ngày: chỉ compare IN/OUT raw count trong cửa sổ chung, skip occupancy.
- Không có "event gap" (khoảng thời gian trong cửa sổ mà camera-ai không nhận event mặc dù DCNET có) — kiểm bằng `GROUP BY date_trunc('hour', ts)`.

### 6.4 Debug nếu fail

- Kiểm log event_collector_cameraai: reconnect loop? parsing error?
- Kiểm `UNIQUE` constraint violation (bình thường — ON CONFLICT DO NOTHING).
- Kiểm clock skew VM vs docker container (TZ env set chính xác).

---

## 7. Rollback

Rollback đơn giản **vì old stack không bị tắt trong giai đoạn parallel** — chỉ cần:

**R1.** Sửa Caddyfile: flip upstream về `dashboard:8501`. Restore `basic_auth` block (cần giữ hash bcrypt hoặc backup Caddyfile). `caddy reload`.

**R2.** Old dashboard vẫn chạy (chưa stop) → live ngay.

**R3.** Nếu đã decommission dashboard (giai đoạn D): `docker start dashboard` (stack DCNET) — container vẫn tồn tại, chỉ stop chứ không xóa. Postgres DCNET vẫn còn data (volume chưa xóa).

**Cửa sổ rollback an toàn:** Tối thiểu 7 ngày sau decommission dashboard (trong thời gian giữ postgres DCNET). Sau khi xóa volume postgres DCNET → rollback không còn data cũ.

**Không thể rollback bằng cách này:** Nếu đã rebind broker sang camera-ai compose (alternate broker migration — §2.1 Open). Đây là lý do chiến lược mặc định giữ mosquitto stack DCNET là shared infra.

---

## 8. Ops sau cutover

### 8.1 Auth

- Mật khẩu admin mặc định `admin/admin` **phải đổi trước flip** (bước A6).
- FDW tự quản lý user qua bảng `users` (Postgres) — tạo thêm user qua `/admin/users` nếu FDW có route đó, hoặc INSERT trực tiếp (password_hash = bcrypt).
- **Không còn Caddy `basic_auth`** sau flip — FDW là **tuyến bảo vệ duy nhất**. Tất cả route (bao gồm `/api/counting`, `/api/events`, bất kỳ `/api/*`) phải enforce JWT auth tại app layer. Không có route nào trả data mà không check token. `SECRET_KEY` phải random ≥ 32 bytes (không dùng default).
- `/live/*` và `/cam/*`: xem §2.7 + Open O9 — cần auth gate cụ thể trước khi expose.

### 8.2 Domain / TLS

- DNS `camera-test.dcnet.vn` → `163.227.121.206` — **không đổi**.
- Caddy LE cert vẫn renew tự động (challenge port 80 open). Mosquitto tiếp tục mượn cert qua `cert-sync.sh` (logic không thay đổi).
- **Không cần LE cert mới** — Caddy hiện tại đã có cert cho domain, cert thuộc Caddy stack DCNET.

### 8.3 go2rtc live view

- RTSP stream: `rtsp://[cam_user]:[cam_pass]@115.79.47.96:554/axis-media/media.amp`.
- VM (`163.227.121.206`) reach cam qua `115.79.47.96:554` — **Open: verify VM IP có trong allowlist cam RTSP port 554 không** (cam NAT công khai nhưng có thể team system siết IP). Camera-state xác nhận 8443 reach từ VM; 554 chưa verify.
- go2rtc expose WebSocket qua Caddy `/live/*` — không publish port 1984 ra host. **Không thêm ufw rule mới** (giữ nguyên `ports_public: [22, 80, 443, 8883]`).
- go2rtc config: RTSP credentials trong `.env` (không hardcode), mount vào container.

### 8.4 Firewall

**Không thay đổi ufw** — giữ nguyên port mở hiện tại `[22, 80, 443, 8883]`. Mọi service nội bộ (postgres, fall_detection_web, go2rtc, event_collector) chỉ expose qua docker network, không qua host port.

### 8.5 Backup

- **Postgres camera-ai:** cron `pg_dump dcnet | gzip > /opt/backup/dcnet_$(date +%Y%m%d).sql.gz` — tần suất hàng ngày, giữ 7 ngày. **Open: VM có cron setup không? Cần script riêng hay tích hợp vào Makefile/deploy skill.**
- **Postgres DCNET (cũ):** giữ volume `postgres_data` DCNET tối thiểu 7 ngày post-decommission dashboard. Không cần backup thêm.
- **go2rtc config:** mount từ file trong repo (gitignore credentials), rebuild là đủ.
- **FDW data volume:** `fdw_data` (event images, teldrive_cache) — nếu có data quan trọng, backup tương tự pg_dump.

### 8.6 Monitoring

- `docker compose -f docker-compose.prod.yml ps` + logs — monitoring thủ công hiện tại (như DCNET cũ).
- event_collector log JSON (`structlog`) → kiểm INSERT rate sau flip.
- **Open:** alert/uptime monitor (DCNET chưa có) — ngoài scope Phase 4.

---

## 9. Contrast với prod stack DCNET hiện tại

| Dimension | DCNET prod (hiện tại) | camera-ai prod (đích) |
|---|---|---|
| Dashboard | Streamlit 1.39 (:8501) | FastAPI/Jinja (:8090) |
| Auth | Caddy basic_auth → X-Auth-User header | JWT login form (FDW tự quản lý) |
| DB | Postgres `dcnet_camera` | Postgres `dcnet` (greenfield) |
| MQTT client-id | `event_collector` | `event_collector_cameraai` |
| Live view | cam_proxy nginx → cam NAT 8443 | go2rtc RTSP → cam NAT 554 + WebSocket |
| Caddy route chính | `/` → `dashboard:8501` | `/` → `fall_detection_web:8090` |
| Re-ID | Không (đã gate NO-GO) | reid_worker profile-gated off |
| Mosquitto | Service trong stack (8883 public) | **Shared infra từ stack DCNET** |
| Compose file | `docker-compose.prod.yml` (camera repo) | `docker-compose.prod.yml` (camera-ai repo) |
| Repo trên VM | `/opt/camera` | `/opt/camera-ai` (**Open: confirm**) |

---

## 10. Open decisions (confirm khi review)

| # | Vấn đề | Đề xuất | Hành động cần |
|---|---|---|---|
| O1 | Thư mục clone camera-ai trên VM | `/opt/camera-ai` | Confirm với team system trước deploy |
| O2 | Tên compose project stack DCNET trên VM | Phụ thuộc `docker compose -p` hoặc dir name | `docker compose ls` trên VM để biết prefix network |
| O3 | VM có đủ RAM/disk cho 2× postgres đồng thời? | Postgres nhẹ (counting rows only); likely OK nhưng cần verify | `free -h` + `df -h` trên VM trước A5 |
| O4 | RTSP port 554 có trong allowlist cam từ VM IP? | Likely có (team system mở cùng lúc với 8443) nhưng chưa verify chính thức | Test `nc -zv 115.79.47.96 554` từ VM |
| O5 | go2rtc: image version + config format `go2rtc.yaml` | Image `alexxit/go2rtc:latest` hoặc pin version | Confirm version khi soạn plan Phase 4 |
| O6 | Backup cron trên VM | Cần script; chưa có cron setup | Thêm vào deploy checklist |
| O7 | Broker migration (alternate — §2.1) | Defer hậu-cutover; không làm trong Phase 4 | Review sau khi camera-ai ổn định ≥ 30 ngày |
| O8 | Thêm user viewer ngoài admin sau cutover | FDW cần UI hoặc INSERT trực tiếp | Confirm FDW có `/admin/users` route ở Phase 3 không |
| O9 | Auth gate cho `/live/*` và `/cam/*` sau khi bỏ Caddy basic_auth | (a) FDW proxy nội bộ + JWT check, hoặc (b) Caddy `forward_auth` → FDW `/api/auth/check` | Thiết kế + implement trước flip (block cutover nếu chưa có) |

---

## 11. Rủi ro & giảm thiểu

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| Mosquitto tắt nhầm khi stop stack DCNET | **CAO** | Stop selective (dashboard + event_collector chỉ), không `docker compose down` toàn stack DCNET. Mosquitto service của DCNET tiếp tục. |
| `/cam/*` hoặc `/live/*` lộ unauthenticated sau bỏ basic_auth | **CAO** | Bắt buộc implement auth gate (O9) trước flip. Step C1 ban hành: không flip nếu O9 chưa giải quyết. Verify §12 step 6: unauthenticated request phải 401. |
| Schema race: event_collector INSERT trước FDW init_db | **CAO** | Thêm `db/init.sql` mount vào postgres — schema tạo trong postgres init, trước mọi service start. |
| Admin password chưa đổi trước flip | Cao | Bước A6 bắt buộc, block flip nếu chưa làm. |
| RTSP 554 không reach → live view offline | Trung | Go2rtc log ngay khi start; fallback cam_proxy/8443 giữ chạy (§2.7). Live view failure không ảnh hưởng counting. |
| Parity fail do message loss | Trung | Parallel window ≥ 1 ngày; debug log trước khi flip; rollback đơn giản. |
| go2rtc WebSocket bị Caddy timeout | Thấp | Set `timeout_read_client` phù hợp trong Caddy (hoặc `transport` websocket header). |
| 2× postgres RAM pressure | Thấp | Counting DB nhỏ (rows sự kiện); verify headroom (O3) trước. |

---

## 12. Verification sau cutover

1. `https://camera-test.dcnet.vn/` → login form FDW (không phải Streamlit). Login OK.
2. Trang đếm: occupancy + IN/OUT hôm nay hiển thị, auto-refresh cập nhật khi có crossing.
3. Live view (`/live/` hoặc trang Camera): stream go2rtc load.
4. `docker compose logs event_collector` (camera-ai): INSERT log chạy đều, không reconnect loop.
5. `docker compose ps` (camera-ai): tất cả service `Up (healthy)`.
6. **Auth gate verify:** `curl -s -o /dev/null -w "%{http_code}" https://camera-test.dcnet.vn/cam/snapshot` (không auth) → phải trả **401 hoặc redirect login**, KHÔNG phải 200. Tương tự `/api/counting` không có JWT token → 401. Bất kỳ route nào trả 200 mà không có credentials = security regression, block decommission.
7. Mosquitto 8883: `openssl s_client -connect camera-test.dcnet.vn:8883` → cert valid. Cam vẫn publish (kiểm event row tăng sau crossing).
8. Caddy cert renew: `caddy reload` không lỗi; `/etc/caddy/` cert path vẫn thuộc stack DCNET.

---

## 13. Decompose (cho writing-plans / dcnet-deploy)

1. **Prod compose file** — soạn `camera-ai/docker-compose.prod.yml` (Phase 1: postgres + fdw + event_collector; Phase 4 thêm go2rtc + reid_worker profile). **Kèm `db/init.sql`** (dump schema Phase 0–3) mount vào postgres — giải quyết schema race (§4.2).
2. **go2rtc config** — `go2rtc.yaml` với RTSP stream cam + creds từ env.
3. **Network setup** — `docker network create dcnet-shared` + thêm network vào Caddyfile/compose DCNET.
4. **Auth gate cho /live/* + /cam/**** — implement O9 (FDW proxy hoặc Caddy forward_auth) trước khi soạn Caddyfile post-flip. Block flip nếu chưa xong.
5. **Caddyfile target** — draft Caddyfile post-flip (bỏ basic_auth site-wide, route / → FDW JWT, route /live/* → go2rtc có auth gate, /cam/* qua FDW hoặc disabled).
6. **Deploy + parity run** — actual VM operations via `dcnet-deploy` skill.
7. **Decommission checklist** — after ≥1 ngày ổn định: stop dashboard → stop event_collector DCNET → archive postgres DCNET.

---
## Liên quan
- Tổng thể: [migration design](2026-06-26-dcnet-platform-migration-design.md)
- Trước: [Phase 3 Modular](2026-06-26-phase3-modular-percustomer-design.md) · Sau: (cuối — sau cutover platform hợp nhất xong)
