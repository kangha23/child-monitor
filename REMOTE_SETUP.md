# WebRTC Remote Desktop — Hướng dẫn Cài đặt Signaling Server trên Mac

## Tổng quan Kiến trúc

```
Máy mục tiêu (Windows)          Mac (Signaling Server)           Trình duyệt (Viewer)
   SystemHelper.exe  ──────►  cloudflared tunnel  ◄──────  Mở link Web Viewer
   (WebRTC Host)              wss://xxx.trycloudflare.com       (WebRTC Viewer)
                                    │
                              signaling_server.py
                              (ws://localhost:8765)
```

## Bước 1: Cài đặt trên Mac

```bash
# 1. Cài Python dependencies
pip3 install websockets

# 2. Cài Cloudflare Tunnel (miễn phí, không cần tài khoản)
brew install cloudflared

# 3. Clone repo hoặc copy file signaling_server.py và thư mục web_viewer/ sang Mac
git clone https://github.com/kangha23/child-monitor.git
cd child-monitor
```

## Bước 2: Chạy Signaling Server

Terminal 1 — Chạy server:
```bash
python3 signaling_server.py
```

Terminal 2 — Mở tunnel (lấy URL công khai):
```bash
cloudflared tunnel --url http://localhost:8765
```

Cloudflared sẽ in ra URL dạng:
```
https://random-name-here.trycloudflare.com
```

**Copy URL này** — đây là địa chỉ signaling server công khai.

## Bước 3: Cập nhật Config trên máy mục tiêu

Sửa `config.json` trên máy mục tiêu:
```json
{
  "webrtc": {
    "signaling_server": "wss://random-name-here.trycloudflare.com",
    "viewer_base_url": "https://random-name-viewer.trycloudflare.com",
    ...
  }
}
```

Hoặc dùng lệnh `/update` trên Telegram sau khi push config mới lên GitHub.

## Bước 4: Mở Tunnel cho Web Viewer (Terminal 3)

```bash
cloudflared tunnel --url http://localhost:8088
```

Copy URL tunnel thứ 2 này — đây là link Web Viewer công khai.

## Bước 5: Sử dụng

1. Gõ `/remote` trong Telegram
2. Bot sẽ trả về link viewer với Room ID
3. Mở link Web Viewer tunnel + thêm `#room_id&signaling=wss://signaling-tunnel-url`
4. Xem màn hình và điều khiển từ xa!

## Lưu ý

- Mỗi lần chạy `cloudflared tunnel`, URL sẽ thay đổi (trừ khi dùng Named Tunnel với tài khoản Cloudflare)
- Để URL cố định, tạo tài khoản Cloudflare miễn phí và dùng Named Tunnel
- Signaling server chỉ relay tín hiệu, video stream đi trực tiếp P2P (hoặc qua TURN nếu bị NAT)
