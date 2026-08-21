#!/usr/bin/env python3
"""
WebRTC Signaling Relay Server
Chạy trên Mac để làm trung gian kết nối WebRTC giữa Host (máy mục tiêu) và Viewer (trình duyệt).

Cách dùng:
  1. pip install websockets
  2. python signaling_server.py
  3. Mở tunnel: cloudflared tunnel --url http://localhost:8765

Server lắng nghe trên port 8765 (WebSocket) và 8088 (Web Viewer HTTP).
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SignalingServer")

# Lưu các room: room_id -> {"host": ws, "viewers": [ws, ...]}
rooms = {}


async def handle_connection(websocket):
    """Xử lý mỗi kết nối WebSocket."""
    room_id = None
    role = None

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")
            msg_room = data.get("room")

            if msg_type == "join":
                room_id = msg_room
                role = data.get("role", "viewer")

                if room_id not in rooms:
                    rooms[room_id] = {"host": None, "viewers": []}

                if role == "host":
                    rooms[room_id]["host"] = websocket
                    logger.info(f"🖥️  Host joined room: {room_id}")
                    # Thông báo cho tất cả Viewers đang chờ biết Host đã sẵn sàng
                    for v in rooms[room_id]["viewers"]:
                        try:
                            await v.send(json.dumps({"type": "ready"}))
                        except Exception:
                            pass
                else:
                    rooms[room_id]["viewers"].append(websocket)
                    logger.info(f"👁️  Viewer joined room: {room_id} (total: {len(rooms[room_id]['viewers'])})")
                    # Nếu Host đã ở trong phòng, báo cho Viewer này biết để tạo Offer ngay lập tức
                    if rooms[room_id]["host"]:
                        try:
                            await websocket.send(json.dumps({"type": "ready"}))
                        except Exception:
                            pass

            elif msg_type == "offer":
                # Viewer gửi offer -> chuyển tiếp cho Host
                if room_id and room_id in rooms and rooms[room_id]["host"]:
                    await rooms[room_id]["host"].send(message)
                    logger.info(f"📨 Offer forwarded to host in room: {room_id}")

            elif msg_type == "answer":
                # Host gửi answer -> chuyển tiếp cho tất cả Viewer
                if room_id and room_id in rooms:
                    for viewer in rooms[room_id]["viewers"]:
                        try:
                            await viewer.send(message)
                        except Exception:
                            pass
                    logger.info(f"📨 Answer forwarded to viewers in room: {room_id}")

            elif msg_type == "candidate":
                # ICE candidate -> chuyển tiếp cho phía bên kia
                if room_id and room_id in rooms:
                    if role == "host":
                        for viewer in rooms[room_id]["viewers"]:
                            try:
                                await viewer.send(message)
                            except Exception:
                                pass
                    elif rooms[room_id]["host"]:
                        try:
                            await rooms[room_id]["host"].send(message)
                        except Exception:
                            pass

            else:
                # Chuyển tiếp các loại tin nhắn khác
                if room_id and room_id in rooms:
                    if role == "host":
                        for viewer in rooms[room_id]["viewers"]:
                            try:
                                await viewer.send(message)
                            except Exception:
                                pass
                    elif rooms[room_id]["host"]:
                        try:
                            await rooms[room_id]["host"].send(message)
                        except Exception:
                            pass

    except Exception as e:
        logger.warning(f"Connection closed: {e}")
    finally:
        # Cleanup khi disconnect
        if room_id and room_id in rooms:
            if role == "host":
                rooms[room_id]["host"] = None
                logger.info(f"🖥️  Host left room: {room_id}")
            elif websocket in rooms[room_id]["viewers"]:
                rooms[room_id]["viewers"].remove(websocket)
                logger.info(f"👁️  Viewer left room: {room_id}")

            # Xóa room nếu trống
            if rooms[room_id]["host"] is None and len(rooms[room_id]["viewers"]) == 0:
                del rooms[room_id]
                logger.info(f"🗑️  Room deleted: {room_id}")


async def start_websocket_server(host="0.0.0.0", port=8765):
    """Khởi động WebSocket Signaling Server."""
    try:
        import websockets
    except ImportError:
        logger.error("Cần cài đặt websockets: pip install websockets")
        sys.exit(1)

    server = await websockets.serve(handle_connection, host, port)
    logger.info(f"🚀 Signaling Server đang chạy tại ws://{host}:{port}")
    logger.info(f"")
    logger.info(f"📋 Hướng dẫn tiếp theo:")
    logger.info(f"   1. Mở terminal mới và chạy: cloudflared tunnel --url http://localhost:8765")
    logger.info(f"   2. Copy URL tunnel (dạng https://xxx.trycloudflare.com)")
    logger.info(f"   3. Cập nhật config.json trên máy mục tiêu:")
    logger.info(f'      "signaling_server": "wss://xxx.trycloudflare.com"')
    logger.info(f"")
    await server.wait_closed()


# ==========================================
# Web Viewer HTTP Server (chạy song song)
# ==========================================

async def serve_web_viewer(host="0.0.0.0", port=8088):
    """HTTP server phục vụ trang Web Viewer."""
    from http.server import SimpleHTTPRequestHandler
    import http.server
    import threading

    viewer_dir = Path(__file__).parent / "web_viewer"
    if not viewer_dir.exists():
        logger.warning(f"Thư mục web_viewer không tồn tại: {viewer_dir}")
        return

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(viewer_dir), **kwargs)
        def log_message(self, format, *args):
            return

    httpd = http.server.HTTPServer((host, port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    logger.info(f"🌐 Web Viewer đang chạy tại http://{host}:{port}")


async def main():
    await serve_web_viewer(port=8088)
    await start_websocket_server(port=8765)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server đã dừng.")
