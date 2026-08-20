#!/usr/bin/env python3
"""
Lightweight WebRTC Signaling Relay Server
Chạy độc lập trên VPS hoặc máy chủ để làm cầu nối SDP/ICE và quản lý phòng kết nối WebRTC P2P/TURN.
"""

import asyncio
import json
import logging
import os
import sys

try:
    import websockets
except ImportError:
    print("Vui lòng cài đặt websockets: pip install websockets")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SignalingRelay")

# Lưu trữ các phòng: room_id -> {"host": websocket, "viewers": set(websocket)}
ROOMS = {}


async def handle_client(websocket):
    current_room = None
    role = None
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except Exception:
                continue

            msg_type = data.get("type")
            room_id = data.get("room")

            if msg_type == "join":
                current_room = room_id
                role = data.get("role", "viewer")
                if current_room not in ROOMS:
                    ROOMS[current_room] = {"host": None, "viewers": set()}

                if role == "host":
                    ROOMS[current_room]["host"] = websocket
                    logger.info(f"Host joined room: {current_room}")
                    # Báo cho các viewer đang chờ
                    for v in list(ROOMS[current_room]["viewers"]):
                        try:
                            await v.send(json.dumps({"type": "ready", "room": current_room}))
                        except Exception:
                            pass
                else:
                    ROOMS[current_room]["viewers"].add(websocket)
                    logger.info(f"Viewer joined room: {current_room}")
                    # Nếu host đã có sẵn trong phòng, báo ready
                    if ROOMS[current_room]["host"]:
                        await websocket.send(json.dumps({"type": "ready", "room": current_room}))

            elif msg_type in ["offer", "answer", "candidate"]:
                if not current_room or current_room not in ROOMS:
                    continue

                room_data = ROOMS[current_room]
                if role == "host":
                    # Chuyển tiếp từ host tới tất cả viewer
                    for v in list(room_data["viewers"]):
                        try:
                            await v.send(message)
                        except Exception:
                            pass
                else:
                    # Chuyển tiếp từ viewer tới host
                    if room_data["host"]:
                        try:
                            await room_data["host"].send(message)
                        except Exception:
                            pass

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if current_room and current_room in ROOMS:
            room_data = ROOMS[current_room]
            if role == "host" and room_data["host"] == websocket:
                room_data["host"] = None
                logger.info(f"Host left room: {current_room}")
                for v in list(room_data["viewers"]):
                    try:
                        await v.send(json.dumps({"type": "peer_left", "role": "host"}))
                    except Exception:
                        pass
            elif role == "viewer" and websocket in room_data["viewers"]:
                room_data["viewers"].remove(websocket)
                logger.info(f"Viewer left room: {current_room}")

            if not room_data["host"] and len(room_data["viewers"]) == 0:
                del ROOMS[current_room]


async def main():
    port = int(os.environ.get("PORT", 8765))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"Khởi động WebRTC Signaling Relay tại ws://{host}:{port}")
    async with websockets.serve(handle_client, host, port):
        await asyncio.Future()  # Chạy mãi mãi


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Đã dừng Signaling Relay.")
