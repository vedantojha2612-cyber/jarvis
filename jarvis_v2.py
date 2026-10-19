import sys, json, subprocess, os, paramiko, time, urllib.parse, webbrowser
import threading, wikipedia, re, psutil, asyncio, datetime, base64, ctypes
from collections import deque
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
try:
    from miraie_ac import MirAIeHub, MirAIeBroker
except ImportError:
    MirAIeHub, MirAIeBroker = None, None

try:
    import requests as http_req
except ImportError:
    print("[WARN] pip install requests")
    http_req = None
try:
    from dotenv import load_dotenv
    _env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env_file):
        load_dotenv(_env_file)
    load_dotenv()
except ImportError:
    pass

def ensure_desktop_attached():
    """Ensure current process/thread is attached to interactive desktop WinSta0\\Default."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        h_winsta = user32.OpenWindowStationW("WinSta0", False, 0x037F)
        if h_winsta:
            user32.SetProcessWindowStation(h_winsta)
        h_desk = user32.OpenDesktopW("default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
        user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass

if sys.platform == "win32":
    ensure_desktop_attached()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

app = Flask(__name__)
CORS(app)

# ---- CREDENTIALS (.env) ----
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
PC_IP            = os.getenv("PC_IP", "192.168.1.10")
LAPTOP_IP        = os.getenv("LAPTOP_IP", "192.168.1.6")
PC_USER          = os.getenv("PC_USER", "jay")
PC_PASS          = os.getenv("PC_PASS", "")
MIRAIE_MOBILE    = os.getenv("MIRAIE_MOBILE", "")
MIRAIE_PASS      = os.getenv("MIRAIE_PASS", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEATHER_LAT      = os.getenv("WEATHER_LAT", "19.0760")
WEATHER_LON      = os.getenv("WEATHER_LON", "72.8777")

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"[WARN] Gemini Client init failed: {e}")
        client = None

GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite"
]

def call_gemini(contents):
    if client is None:
        raise RuntimeError("Gemini API key is not configured or client failed to initialize.")
    from google.genai import types
    cfg = types.GenerateContentConfig(temperature=0.2)
    last_err = None
    for m in GEMINI_MODELS:
        try:
            return client.models.generate_content(model=m, contents=contents, config=cfg)
        except Exception as e:
            last_err = e
            err_str = str(e)
            print(f"[GEMINI FALLBACK] Model '{m}' error: {err_str[:90]}. Trying next fallback model...")
            time.sleep(0.3)
            continue
    print(f"[GEMINI ERROR] All fallback models failed. Last error: {last_err}")
    raise last_err or RuntimeError("No available Gemini models responded.")


# ---- FILE PATHS ----
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
NOTES_FILE     = os.path.join(BASE_DIR, "jarvis_notes.json")
GOALS_FILE     = os.path.join(BASE_DIR, "jarvis_goals.json")
ANALYTICS_FILE = os.path.join(BASE_DIR, "jarvis_analytics.json")
SCREENSHOT_DIR = os.path.join(BASE_DIR, "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

_tpl = os.path.join(BASE_DIR, "template.html")
HTML = open(_tpl, encoding="utf-8").read() if os.path.exists(_tpl) else "<h1>template.html not found</h1>"

# ---- GLOBALS ----
memory_lock     = threading.Lock()
chat_memory     = deque(maxlen=40)   # 20 pairs = richer context
history_lock    = threading.Lock()
command_history = deque(maxlen=100)
reminders_lock  = threading.Lock()
reminders       = []
_rid            = 0
ac_state_lock   = threading.Lock()
ac_state        = {"power": "unknown", "temp": 24}
ac_connected    = False
_news_cache     = {"data": [], "t": 0}   # news cache (30-min TTL)

# ── Scene definitions ─────────────────────────────────────────
SCENES = {
    "movie night": {
        "steps": [
            ("ac_temp",  26),
            ("ac_power", "on"),
            ("volume",   "down"),  # lower volume 3×
            ("website",  "netflix"),
        ],
        "reply": "Movie Night activated! Netflix opened, AC at 26°C, volume lowered. Enjoy! 🎬"
    },
    "study mode": {
        "steps": [
            ("ac_temp",  24),
            ("ac_power", "on"),
            ("open_study_urls", None),
        ],
        "reply": "Study Mode on! AC 24°C, opening PW · MolView · Gemini · ChatGPT. Focus! 📚"
    },
    "sleep mode": {
        "steps": [
            ("ac_temp",  26),
            ("ac_power", "on"),
            ("volume",   "mute"),
            ("announce", "Goodnight! Sleep well, Heisenberg."),
        ],
        "reply": "Sleep Mode activated. AC 26°C, muted, goodnight announced. 🌙"
    },
    "focus mode": {
        "steps": [
            ("ac_temp",  22),
            ("ac_power", "on"),
            ("volume",   "mute"),
        ],
        "reply": "Focus Mode on! AC 22°C, all audio muted. Deep work time. 🎯"
    },
    "morning routine": {
        "steps": [
            ("ac_power", "off"),
            ("announce", "Good morning Heisenberg! Time to crush the day."),
            ("website",  "gmail"),
        ],
        "reply": "Morning routine! AC off, Gmail opened, good morning announced. ☀️"
    },
    "gaming mode": {
        "steps": [
            ("ac_temp",  20),
            ("ac_power", "on"),
            ("volume",   "up"),
        ],
        "reply": "Gaming Mode on! AC 20°C for peak performance, volume maxed. 🎮"
    },
}

SYSTEM_PROMPT = """
You are Jarvis, an intelligent home automation AI.
You control "pc" (Linux/Zorin via SSH) and "laptop" (Windows), plus a Panasonic AC.
Output ONLY valid JSON: {"response_text":"...", "actions":[{"action":"...","device":"...","target":"..."}]}
Actions: shutdown_pc, sleep_pc, close_app, open_app,
  media_play_pause, media_next, media_prev, volume_up, volume_down, volume_mute, ac_power, ac_temp
"""

BRAVE_WIN = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe")
CHROME_WIN = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE_WIN = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

WINDOWS_OPEN = {
    "calculator": "calc.exe", "calc": "calc.exe", "settings": "ms-settings:",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "edge": EDGE_WIN if os.path.exists(EDGE_WIN) else "msedge",
    "brave": BRAVE_WIN if os.path.exists(BRAVE_WIN) else "brave",
    "photos": "ms-photos:",
    "chrome": CHROME_WIN if os.path.exists(CHROME_WIN) else "chrome",
    "notepad": "notepad.exe", "paint": "mspaint.exe", "vlc": "vlc.exe",
    "system monitor": "taskmgr.exe", "task manager": "taskmgr.exe",
    "file explorer": "explorer.exe", "files": "explorer.exe", "explorer": "explorer.exe",
    "spotify": "spotify:", "discord": "discord:",
    "terminal": "wt.exe", "cmd": "cmd.exe", "powershell": "powershell.exe",
}
WINDOWS_CLOSE = {
    "calculator": "CalculatorApp.exe", "calc": "CalculatorApp.exe", "settings": "SystemSettings.exe",
    "word": "WINWORD.exe", "excel": "EXCEL.exe", "powerpoint": "POWERPNT.exe", "edge": "msedge.exe",
    "brave": "brave.exe", "photos": "Microsoft.Photos.exe", "chrome": "chrome.exe", "notepad": "notepad.exe",
    "paint": "mspaint.exe", "vlc": "vlc.exe", "system monitor": "Taskmgr.exe", "task manager": "Taskmgr.exe",
    "spotify": "Spotify.exe", "discord": "Discord.exe",
}
LINUX_OPEN = {
    "calculator": "gnome-calculator", "calc": "gnome-calculator", "settings": "gnome-control-center",
    "chrome": "brave-browser", "brave": "brave-browser", "vlc": "vlc",
    "spotify": "spotify", "discord": "discord", "files": "nautilus",
    "file manager": "nautilus", "system monitor": "gnome-system-monitor",
    "task manager": "gnome-system-monitor", "terminal": "gnome-terminal", "text editor": "gedit",
}
LINUX_CLOSE = {
    "calculator": "gnome-calculator", "calc": "gnome-calculator", "settings": "gnome-control-center",
    "chrome": "brave", "brave": "brave", "vlc": "vlc", "spotify": "spotify", "discord": "discord",
    "system monitor": "gnome-system-monitor", "task manager": "gnome-system-monitor",
}
WEBSITES = {
    "youtube": "https://www.youtube.com", "swiggy": "https://www.swiggy.com/instamart",
    "github": "https://github.com", "whatsapp": "https://web.whatsapp.com",
    "pw": "https://www.pw.live", "physics wallah": "https://www.pw.live",
    "chemistry 3d": "https://molview.org", "molview": "https://molview.org",
    "gemini": "https://gemini.google.com", "chatgpt": "https://chatgpt.com",
    "google": "https://www.google.com", "netflix": "https://www.netflix.com",
    "instagram": "https://www.instagram.com", "twitter": "https://twitter.com",
    "reddit": "https://www.reddit.com", "wikipedia": "https://www.wikipedia.org",
    "gmail": "https://mail.google.com", "drive": "https://drive.google.com",
    "meet": "https://meet.google.com", "maps": "https://maps.google.com",
    "prime": "https://www.primevideo.com", "hotstar": "https://www.hotstar.com",
}

# ==========================================
# AC CONTROL
# ==========================================
ac_loop = asyncio.new_event_loop()
threading.Thread(target=lambda:(asyncio.set_event_loop(ac_loop), ac_loop.run_forever()), daemon=True).start()
ac_broker = None
ac_hub    = None

async def _connect_ac():
    global ac_broker, ac_hub, ac_connected
    if MirAIeBroker is None or MirAIeHub is None:
        print("[JARVIS] MirAIe AC library not loaded, skipping direct cloud connect.")
        return
    try:
        print("\n[JARVIS] Connecting to AC...")
        ac_broker = MirAIeBroker(); ac_hub = MirAIeHub()
        await ac_hub.init(MIRAIE_MOBILE, MIRAIE_PASS, ac_broker)
        for _ in range(15):
            if hasattr(ac_broker,"client") and ac_broker.client is not None: break
            await asyncio.sleep(1)
        ac_connected = True; print("[JARVIS] AC connected!\n")
    except Exception as e:
        print(f"[JARVIS] AC init failed: {e}\n")

asyncio.run_coroutine_threadsafe(_connect_ac(), ac_loop)

def execute_ac_command(cmd_type, value=None):
    async def _run():
        if not ac_connected or ac_hub is None: return "AC cloud warming up. Retry shortly."
        if not ac_hub.home.devices: return "No AC found on MirAIe account."
        ac = ac_hub.home.devices[0]
        if cmd_type == "power":
            if value == "on":
                await ac.turn_on()
                with ac_state_lock: ac_state["power"] = "on"
                return "AC turned on."
            else:
                await ac.turn_off()
                with ac_state_lock: ac_state["power"] = "off"
                return "AC turned off."
        elif cmd_type == "temp":
            t = max(16, min(30, int(value)))
            await ac.set_temperature(t)
            with ac_state_lock: ac_state["temp"] = t
            return f"AC temperature set to {t} degrees."
    try:
        return asyncio.run_coroutine_threadsafe(_run(), ac_loop).result(timeout=10)
    except Exception as e:
        return f"AC command failed: {e}"

# ==========================================
# PC / LAPTOP CONTROL
# ==========================================
_GUI = ("export DISPLAY=:0 && export WAYLAND_DISPLAY=wayland-0 && "
        "export XDG_RUNTIME_DIR=/run/user/1000 && "
        "export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus && ")

def execute_on_pc(cmd):
    if sys.platform != "win32":
        try:
            p = subprocess.Popen(_GUI + cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            o, e = p.communicate(timeout=8)
            out = o.decode(errors="ignore").strip()
            err = e.decode(errors="ignore").strip()
            if err: print(f"[Local PC] {err}")
            return out or "Done."
        except Exception as ex:
            return f"Local Exec Error: {ex}"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(PC_IP, username=PC_USER, password=PC_PASS, timeout=5)
        _, o, e = ssh.exec_command(_GUI + cmd, timeout=8)
        out = o.read().decode().strip(); err = e.read().decode().strip()
        ssh.close()
        if err: print(f"[PC] {err}")
        return out or "Done."
    except Exception as ex: return f"SSH Error: {ex}"

def execute_pc_terminal(cmd):
    t0 = time.time()
    if sys.platform != "win32":
        try:
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            o, e = p.communicate(timeout=15)
            out = o.decode(errors="ignore").strip()
            err = e.decode(errors="ignore").strip()
            ms = int((time.time() - t0) * 1000)
            return {
                "output": out or err or "Command executed with no output.",
                "stdout": out,
                "stderr": err,
                "error": err if p.returncode != 0 else "",
                "code": p.returncode,
                "ms": ms
            }
        except Exception as ex:
            return {"output": "", "stdout": "", "error": f"Local Error: {ex}", "stderr": f"Local Error: {ex}", "code": 1, "ms": int((time.time() - t0) * 1000)}
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    t0 = time.time()
    try:
        ssh.connect(PC_IP, username=PC_USER, password=PC_PASS, timeout=6)
        _, o, e = ssh.exec_command(cmd, timeout=15)
        out = o.read().decode("utf-8", errors="replace").strip()
        err = e.read().decode("utf-8", errors="replace").strip()
        code = o.channel.recv_exit_status()
        ssh.close()
        ms = round((time.time() - t0) * 1000)
        return {"output": out, "stdout": out, "error": err, "stderr": err, "code": code, "exit_code": code, "ms": ms}
    except Exception as ex:
        return {"output": "", "stdout": "", "error": f"SSH Error: {ex}", "stderr": f"SSH Error: {ex}", "code": 1, "exit_code": 1, "ms": round((time.time() - t0) * 1000)}

def get_pc_stats():
    try:
        cpu = execute_on_pc("top -bn1 | grep 'Cpu(s)' | awk '{print $2+$4}'")
        ram = execute_on_pc("free | awk '/Mem/{printf \"%.0f\",$3/$2*100}'")
        dsk = execute_on_pc("df / | awk 'NR==2{print $5}' | tr -d '%'")
        upt = execute_on_pc("uptime -p")
        return {"cpu":cpu.split(".")[0],"ram":ram,"disk":dsk,"uptime":upt}
    except: return {"cpu":"N/A","ram":"N/A","disk":"N/A","uptime":"N/A"}

def get_laptop_stats():
    drv = os.path.splitdrive(os.getcwd())[0] or "C:"
    upt = str(datetime.timedelta(seconds=int(time.time()-psutil.boot_time())))
    return {"cpu":f"{psutil.cpu_percent(interval=0.5):.0f}",
            "ram":f"{psutil.virtual_memory().percent:.0f}",
            "disk":f"{psutil.disk_usage(drv).percent:.0f}","uptime":upt}

def get_battery():
    b = psutil.sensors_battery()
    if b is not None:
        pct = int(b.percent)
        if b.power_plugged:
            tl = "Charging" if pct < 100 else "Full ⚡"
        elif b.secsleft > 0:
            tl = str(datetime.timedelta(seconds=int(b.secsleft)))
        else:
            tl = "Calculating..."
        return {"percent": pct, "plugged": b.power_plugged, "time_left": tl}
    # Fallback for unusual systems: WMI (longer timeout)
    try:
        r = subprocess.run(
            ["powershell", "-c",
             "Get-WmiObject Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10)
        d = json.loads(r.stdout.strip()) if r.stdout.strip() else {}
        pct = d.get("EstimatedChargeRemaining", -1)
        plugged = d.get("BatteryStatus", 1) in [2, 6, 7, 8, 9]
        if pct >= 0:
            return {"percent": int(pct), "plugged": plugged, "time_left": "Charging" if plugged else "On Battery"}
    except Exception:
        pass
    return {"percent": -1, "plugged": True, "time_left": "Desktop / N/A"}

def get_desktop_startupinfo():
    """Returns STARTUPINFO configured for interactive WinSta0\\Default."""
    if sys.platform != "win32":
        return None
    try:
        si = subprocess.STARTUPINFO()
        si.lpDesktop = r"WinSta0\Default"
        return si
    except Exception:
        return None

def send_media_key(vk_code):
    """Sends native Windows media key directly to active session with zero lag."""
    if sys.platform != "win32":
        return False
    try:
        ensure_desktop_attached()
        KEYEVENTF_KEYUP = 0x0002
        user32 = ctypes.windll.user32
        user32.keybd_event(vk_code, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as e:
        print(f"[MEDIA KEY ERR] {e}")
        return False

def send_key(vk_code):
    """Sends native keyboard virtual key directly to active session."""
    if sys.platform != "win32":
        return False
    try:
        ensure_desktop_attached()
        KEYEVENTF_KEYUP = 0x0002
        user32 = ctypes.windll.user32
        user32.keybd_event(vk_code, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as e:
        print(f"[KEY ERR] {e}")
        return False

def focus_browser_window():
    """Unminimize and bring user's browser (Brave, Chrome, Edge) to top of screen."""
    if sys.platform != "win32":
        return
    try:
        ensure_desktop_attached()
        user32 = ctypes.windll.user32
        user32.AllowSetForegroundWindow(-1)
        h_desk = user32.OpenDesktopW("default", 0, False, 0x01FF)
        if not h_desk:
            return

        def cb(hwnd, lParam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    t = buff.value.lower()
                    if any(b in t for b in ['brave', 'chrome', 'edge', 'youtube', 'firefox']):
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        user32.BringWindowToTop(hwnd)
                        user32.SetForegroundWindow(hwnd)
            return True

        EnumDesktopWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        user32.EnumDesktopWindows(h_desk, EnumDesktopWindowsProc(cb), 0)
    except Exception:
        pass

def open_url(url):
    """Open a URL in the user's active browser on Windows interactive desktop with automatic foreground focus."""
    if sys.platform != "win32":
        try:
            laptop_ip = os.getenv("LAPTOP_IP", "192.168.1.6")
            if http_req:
                r = http_req.post(f"http://{laptop_ip}:5000/run", json={"command": f"open {url} on laptop"}, timeout=5)
                return r.status_code == 200
        except Exception as e:
            print(f"[Remote Laptop URL Error] {e}")
            return False
    ensure_desktop_attached()
    si = get_desktop_startupinfo()

    # 1. Primary: Detected browsers directly with autoplay permission (critical for YouTube video & audio)
    try:
        import psutil
        running_names = {p.info['name'].lower() for p in psutil.process_iter(['name']) if p.info.get('name')}
    except Exception:
        running_names = set()

    brave_path = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe")
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

    candidates = []
    if "brave.exe" in running_names and os.path.exists(brave_path):
        candidates.append(brave_path)
    if "chrome.exe" in running_names and os.path.exists(chrome_path):
        candidates.append(chrome_path)
    if "msedge.exe" in running_names and os.path.exists(edge_path):
        candidates.append(edge_path)

    for b in [brave_path, chrome_path, edge_path]:
        if b not in candidates and os.path.exists(b):
            candidates.append(b)

    for b in candidates:
        try:
            subprocess.Popen([b, "--autoplay-policy=no-user-gesture-required", url], startupinfo=si)
            threading.Thread(target=lambda: (time.sleep(0.8), focus_browser_window()), daemon=True).start()
            return True
        except Exception as e:
            print(f"[BROWSER LAUNCH ERR] {b}: {e}")

    # 2. Windows Shell startfile
    try:
        os.startfile(url)
        threading.Thread(target=lambda: (time.sleep(0.8), focus_browser_window()), daemon=True).start()
        return True
    except Exception:
        pass

    # 3. Explorer fallback
    try:
        subprocess.Popen(f'explorer.exe "{url}"', shell=True, startupinfo=si)
        threading.Thread(target=lambda: (time.sleep(0.8), focus_browser_window()), daemon=True).start()
        return True
    except Exception as e:
        print(f"[EXPLORER LAUNCH ERR] {e}")
        return False

def run_laptop(args, shell=False):
    if sys.platform != "win32":
        try:
            laptop_ip = os.getenv("LAPTOP_IP", "192.168.1.6")
            if not http_req: return False
            if isinstance(args, list) and len(args) >= 3 and args[0] == "taskkill":
                app_name = args[2].replace(".exe", "")
                r = http_req.post(f"http://{laptop_ip}:5000/run", json={"command": f"close {app_name} on laptop"}, timeout=5)
                return r.status_code == 200
            elif isinstance(args, list) and args and args[0] == "shutdown":
                r = http_req.post(f"http://{laptop_ip}:5000/run", json={"command": "shutdown laptop"}, timeout=5)
                return r.status_code == 200
            elif isinstance(args, str):
                r = http_req.post(f"http://{laptop_ip}:5000/run", json={"command": args}, timeout=5)
                return r.status_code == 200
            return False
        except Exception as e:
            print(f"[Remote Laptop Error] {e}")
            return False
    try:
        ensure_desktop_attached()
        si = get_desktop_startupinfo()
        if not isinstance(args, list):
            subprocess.Popen(args, shell=True, startupinfo=si)
        else:
            subprocess.Popen(args, startupinfo=si)
        return True
    except Exception as e:
        print(f"[Laptop] {e}")
        return False

def extract_device_and_clean(cmd, default_dev=None):
    if default_dev is None:
        default_dev = "laptop" if sys.platform == "win32" else "pc"
    dev = default_dev
    if re.search(r"\b(on pc|in pc|on my pc|in my pc|on the pc|in the pc|to pc|on zorin|in zorin)\b", cmd, re.IGNORECASE):
        dev = "pc"
    elif re.search(r"\b(on laptop|in laptop|on my laptop|in my laptop|on the laptop|in the laptop|to laptop|on windows|in windows)\b", cmd, re.IGNORECASE):
        dev = "laptop"

    cleaned = cmd
    while True:
        new_c = re.sub(r"^(hey\s+jarvis|jarvis|ok\s+jarvis|please|can\s+you\s+please|can\s+you|could\s+you\s+please|could\s+you|just)\s*,?\s*", "", cleaned, flags=re.IGNORECASE)
        if new_c == cleaned:
            break
        cleaned = new_c

    cleaned = re.sub(r"\b(on\s+my\s+laptop|in\s+my\s+laptop|on\s+laptop|in\s+laptop|on\s+the\s+laptop|in\s+the\s+laptop|to\s+laptop|on\s+windows|in\s+windows)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(on\s+my\s+pc|in\s+my\s+pc|on\s+pc|in\s+pc|on\s+the\s+pc|in\s+the\s+pc|to\s+pc|on\s+zorin|in\s+zorin)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(open|launch|start|run|close|kill|terminate|stop)\s+", "", cleaned.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"^(the|app|application|website|site)\s+", "", cleaned.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(app|application|website|site|browser)$", "", cleaned.strip(), flags=re.IGNORECASE)
    return dev, cleaned.strip().lower()

def launch_laptop_app(target):
    t = target.lower().strip()
    if t in WEBSITES:
        return open_url(WEBSITES[t])
    if any(t.startswith(pfx) for pfx in ["http://", "https://", "www."]) or any(t.endswith(suf) for suf in [".com", ".org", ".net", ".io", ".live"]):
        url = t if t.startswith("http") else "https://" + t
        return open_url(url)

    ensure_desktop_attached()
    si = get_desktop_startupinfo()

    app_target = WINDOWS_OPEN.get(t, t)
    if os.path.exists(app_target):
        try:
            subprocess.Popen([app_target], startupinfo=si)
            return True
        except Exception as e:
            print(f"[LAUNCH APP PATH ERR] {e}")

    if ":" in app_target and not (len(app_target) > 2 and app_target[1] == ":" and app_target[2] == "\\"):
        try:
            os.startfile(app_target)
            return True
        except Exception as e:
            print(f"[STARTFILE URI ERR] {e}")

    try:
        subprocess.Popen([app_target], startupinfo=si)
        return True
    except Exception:
        pass

    try:
        os.startfile(app_target)
        return True
    except Exception:
        pass

    try:
        subprocess.Popen(f'start "" "{app_target}"', shell=True, startupinfo=si)
        return True
    except Exception as e:
        print(f"[SHELL START ERR] {app_target}: {e}")
        return False

def execute_action(action, device, target=""):
    t = target.lower().strip()
    if device == "pc":
        if sys.platform != "win32":
            if action == "close_app":
                pkill_t = LINUX_CLOSE.get(t, t)
                try: subprocess.run(f"pkill -f {pkill_t}", shell=True)
                except Exception as ex: print(f"[Local Linux Kill Err] {ex}")
            elif action == "open_app":
                if t in WEBSITES: execute_on_pc(f"nohup brave-browser '{WEBSITES[t]}' > /tmp/j.log 2>&1 &")
                else: execute_on_pc(f"nohup {LINUX_OPEN.get(t, t)} > /tmp/j.log 2>&1 &")
            elif action == "shutdown_pc": execute_on_pc("systemctl poweroff")
            elif action == "sleep_pc":    execute_on_pc("systemctl suspend")
            elif action == "media_play_pause": execute_on_pc("playerctl play-pause")
            elif action == "media_next":       execute_on_pc("playerctl next")
            elif action == "media_prev":       execute_on_pc("playerctl previous")
            elif action == "volume_up":        execute_on_pc("amixer -D pulse sset Master 10%+")
            elif action == "volume_down":      execute_on_pc("amixer -D pulse sset Master 10%-")
            elif action == "volume_mute":      execute_on_pc("amixer -D pulse sset Master toggle")
            elif action == "take_screenshot":  execute_on_pc("scrot ~/jarvis_screenshot.png")
            return True

        d = {"shutdown_pc":"systemctl poweroff","sleep_pc":"systemctl suspend",
             "media_play_pause":"playerctl play-pause","media_next":"playerctl next",
             "media_prev":"playerctl previous","volume_up":"amixer -D pulse sset Master 10%+",
             "volume_down":"amixer -D pulse sset Master 10%-",
             "volume_mute":"amixer -D pulse sset Master toggle",
             "take_screenshot":"scrot ~/jarvis_screenshot.png"}
        if action in d: execute_on_pc(d[action])
        elif action == "close_app": execute_on_pc(f"pkill -f {LINUX_CLOSE.get(t,t)}")
        elif action == "open_app":
            if t in WEBSITES:
                execute_on_pc(f"nohup brave-browser '{WEBSITES[t]}' > /tmp/j.log 2>&1 &")
            else:
                execute_on_pc(f"nohup {LINUX_OPEN.get(t,t)} > /tmp/j.log 2>&1 &")
        return True

    elif device == "laptop":
        if sys.platform != "win32":
            laptop_ip = os.getenv("LAPTOP_IP", "192.168.1.6")
            try:
                cmd_map = {
                    "close_app": f"close {t} on laptop",
                    "open_app": f"open {t} on laptop",
                    "shutdown_pc": "shutdown laptop",
                    "sleep_pc": "sleep laptop",
                    "media_play_pause": "media play pause",
                    "media_next": "media next",
                    "media_prev": "media previous",
                    "volume_up": "volume up",
                    "volume_down": "volume down",
                    "volume_mute": "volume mute",
                    "take_screenshot": "screenshot laptop"
                }
                c = cmd_map.get(action, f"{action} on laptop")
                if http_req:
                    r = http_req.post(f"http://{laptop_ip}:5000/run", json={"command": c}, timeout=5)
                    return r.status_code == 200
                return False
            except Exception as ex:
                print(f"[Remote Laptop Action Err] {ex}")
                return False

        PS = "powershell -c "
        if   action == "shutdown_pc":      run_laptop(["shutdown","/s","/t","0"])
        elif action == "sleep_pc":         run_laptop(["rundll32.exe","powrprof.dll,SetSuspendState","0,1,0"])
        elif action == "close_app":
            exe_to_kill = WINDOWS_CLOSE.get(t, f"{t}.exe")
            try:
                subprocess.run(f"taskkill /f /im {exe_to_kill}", shell=True, capture_output=True)
            except Exception as ex:
                print(f"[KILL ERR] {ex}")
        elif action == "open_app":
            launch_laptop_app(t)
        elif action == "media_play_pause":
            send_media_key(0xB3)
            def _toggle_k():
                time.sleep(0.04)
                send_key(0x4B)
            threading.Thread(target=_toggle_k, daemon=True).start()
        elif action == "media_next":
            send_media_key(0xB0)
        elif action == "media_prev":
            send_media_key(0xB1)
        elif action == "volume_up":        send_media_key(0xAF)
        elif action == "volume_down":      send_media_key(0xAE)
        elif action == "volume_mute":      send_media_key(0xAD)
        elif action == "take_screenshot":
            sp = os.path.join(SCREENSHOT_DIR,"latest.png").replace("\\","/")
            run_laptop(PS+(
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                "$s=[Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                "$b=New-Object Drawing.Bitmap($s.Width,$s.Height);"
                "$g=[Drawing.Graphics]::FromImage($b);"
                "$g.CopyFromScreen($s.Location,[Drawing.Point]::Empty,$s.Size);"
                f"$b.Save('{sp}')"
            ),shell=True)
        return True

# ==========================================
# NEW FEATURES
# ==========================================

# Weather & Air Quality Index (Open-Meteo, 100% free, no key)
_WX = {"d": None, "t": 0}
def get_weather():
    if _WX["d"] and time.time() - _WX["t"] < 600:
        return _WX["d"]
    if http_req is None:
        return {"error": "requests not installed"}
    try:
        r = http_req.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
            "&current=temperature_2m,weathercode,relative_humidity_2m,windspeed_10m&timezone=Asia%2FKolkata",
            timeout=6)
        c = r.json()["current"]
        codes={0:"Clear Sky",1:"Mainly Clear",2:"Partly Cloudy",3:"Overcast",
               45:"Foggy",51:"Drizzle",53:"Drizzle",55:"Heavy Drizzle",
               61:"Light Rain",63:"Rain",65:"Heavy Rain",71:"Snow",73:"Snow",75:"Heavy Snow",
               80:"Showers",81:"Showers",82:"Heavy Showers",95:"Thunderstorm",99:"Thunderstorm"}
        icons={0:"☀️",1:"🌤️",2:"⛅",3:"☁️",45:"🌫️",51:"🌦️",53:"🌧️",55:"🌧️",
               61:"🌧️",63:"🌧️",65:"🌧️",71:"❄️",73:"❄️",75:"❄️",
               80:"🌦️",81:"🌦️",82:"⛈️",95:"⛈️",99:"⛈️"}
        code = c.get("weathercode", 0)

        # Free Air Quality Index (Open-Meteo)
        aqi_info = {"aqi": 40, "pm25": 15.0, "status": "Good"}
        try:
            aq_r = http_req.get(
                f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={WEATHER_LAT}&longitude={WEATHER_LON}&current=european_aqi,pm2_5,pm10",
                timeout=4)
            if aq_r.ok:
                aq_c = aq_r.json().get("current", {})
                e_aqi = aq_c.get("european_aqi", 40)
                pm25 = aq_c.get("pm2_5", 15.0)
                st = "Good" if e_aqi <= 25 else "Fair" if e_aqi <= 50 else "Moderate" if e_aqi <= 75 else "Poor"
                aqi_info = {"aqi": e_aqi, "pm25": round(float(pm25), 1), "status": st}
        except Exception:
            pass

        res = {
            "temp": c["temperature_2m"], "humidity": c["relative_humidity_2m"],
            "wind": c["windspeed_10m"], "condition": codes.get(code, "Unknown"),
            "icon": icons.get(code, "🌡️"), "aqi": aqi_info
        }
        _WX["d"] = res; _WX["t"] = time.time(); return res
    except Exception as e:
        return {"error": str(e)}

# Live News (Google News RSS, 100% free, zero keys)
def get_news():
    if _news_cache["data"] and time.time() - _news_cache["t"] < 1200:
        return _news_cache["data"]
    if http_req is None:
        return []
    try:
        import xml.etree.ElementTree as ET
        r = http_req.get("https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en", timeout=6)
        if r.ok:
            root = ET.fromstring(r.content)
            items = []
            for item in root.findall(".//item")[:10]:
                title_el = item.find("title")
                link_el = item.find("link")
                pub_el = item.find("pubDate")
                title = title_el.text if title_el is not None else ""
                link = link_el.text if link_el is not None else "#"
                pub = pub_el.text[:16] if pub_el is not None and pub_el.text else ""
                if title:
                    items.append({"title": title, "link": link, "time": pub})
            _news_cache["data"] = items
            _news_cache["t"] = time.time()
            return items
    except Exception as e:
        print(f"[NEWS] {e}")
    return _news_cache["data"] or []

# Scene Engine
def execute_scene(scene_name):
    s = scene_name.lower().strip()
    match = None
    for k in SCENES:
        if k in s or s in k:
            match = k; break
    if not match:
        return f"Scene '{scene_name}' not found. Available: {', '.join(SCENES.keys())}"

    scene = SCENES[match]
    print(f"\n[SCENE] Activating '{match}'")

    def _run():
        for action, val in scene["steps"]:
            try:
                if action == "ac_temp":
                    execute_ac_command("temp", val)
                elif action == "ac_power":
                    execute_ac_command("power", val)
                elif action == "volume":
                    dev = "laptop"
                    if val == "down":
                        for _ in range(3):
                            execute_action("volume_down", dev); time.sleep(0.2)
                    elif val == "up":
                        for _ in range(3):
                            execute_action("volume_up", dev); time.sleep(0.2)
                    elif val == "mute":
                        execute_action("volume_mute", dev)
                elif action == "website":
                    if val in WEBSITES:
                        open_url(WEBSITES[val])
                elif action == "announce":
                    announce_via_speakers(val)
                elif action == "open_study_urls":
                    urls = [WEBSITES["pw"], WEBSITES["chemistry 3d"], WEBSITES["gemini"], WEBSITES["chatgpt"]]
                    for i, u in enumerate(urls):
                        open_url(u)
                        time.sleep(1.5 if i == 0 else 1.0)
            except Exception as ex:
                print(f"[SCENE STEP ERR] {ex}")
            time.sleep(0.4)

    threading.Thread(target=_run, daemon=True).start()
    return scene["reply"]

# Study Goals & Daily Target Tracker
def load_goals():
    try:
        with open(GOALS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"streak": 1, "last_active": datetime.date.today().isoformat(), "goals": []}

def save_goals(data):
    with open(GOALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def add_goal(text, subject="General Study"):
    data = load_goals()
    today = datetime.date.today().isoformat()
    gid = int(time.time() * 1000)
    new_goal = {
        "id": gid,
        "text": text,
        "subject": subject,
        "done": False,
        "date": today
    }
    data.setdefault("goals", []).append(new_goal)
    save_goals(data)
    return {"status": "ok", "success": True, "id": gid, "goal": new_goal, "goals": data["goals"]}

def toggle_goal(gid):
    data = load_goals()
    today = datetime.date.today().isoformat()
    target = None
    for g in data.get("goals", []):
        if g.get("id") == gid:
            g["done"] = not g.get("done", False)
            target = g
            if g["done"]:
                last_act = data.get("last_active")
                if last_act != today:
                    data["streak"] = data.get("streak", 0) + 1
                    data["last_active"] = today
            break
    save_goals(data)
    return {"status": "ok", "success": True, "goal": target, "goals": data["goals"], "streak": data.get("streak", 1)}

def delete_goal(gid):
    data = load_goals()
    data["goals"] = [g for g in data.get("goals", []) if g.get("id") != gid]
    save_goals(data)
    return {"status": "deleted", "success": True, "goals": data["goals"]}

# Daily Briefing
def get_daily_briefing():
    now = datetime.datetime.now()
    w = get_weather()
    temp = w.get("temp", "--")
    cond = w.get("condition", "clear")
    aqi_stat = w.get("aqi", {}).get("status", "good")

    news = get_news()
    top_h = news[0]["title"].split(" - ")[0] if news else "All systems operating normally."

    goals_data = load_goals()
    pending = [g["text"] for g in goals_data.get("goals", []) if not g.get("done")]
    streak = goals_data.get("streak", 1)
    if pending:
        pending_str = f"You have {len(pending)} pending study target{'s' if len(pending)>1 else ''}: {', '.join(pending[:2])}. Study streak is {streak} days."
    else:
        pending_str = f"You have crushed all your study targets! Study streak is {streak} days."

    text = (f"Good day Heisenberg! It is {now.strftime('%A, %B %d')}. "
            f"Currently in Mumbai it is {cond} at {temp} degrees Celsius with {aqi_stat} air quality. "
            f"{pending_str} "
            f"Top headline: {top_h}. All laptop, PC, and AC systems are ready.")
    announce_via_speakers(text)
    return text

# Notes
def load_notes():
    try:
        with open(NOTES_FILE,encoding="utf-8") as f: return json.load(f)
    except: return []

def _save_notes(n):
    with open(NOTES_FILE,"w",encoding="utf-8") as f: json.dump(n,f,indent=2)

def add_note(text):
    n=load_notes()
    n.append({"id":int(time.time()*1000),"text":text,"time":datetime.datetime.now().strftime("%b %d, %H:%M")})
    _save_notes(n); return f'Note saved: "{text}"'

def delete_note(nid):
    n=[x for x in load_notes() if x["id"]!=nid]; _save_notes(n); return "Note deleted."

# Study Analytics
def load_analytics():
    try:
        with open(ANALYTICS_FILE,encoding="utf-8") as f: return json.load(f)
    except: return {"sessions":[],"total_hours":0}

def log_study_session(hours, device, subject="General Study", notes=""):
    d = load_analytics()
    d["sessions"].append({
        "date": datetime.datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.datetime.now().strftime("%H:%M"),
        "hours": hours,
        "device": device,
        "subject": subject,
        "notes": notes
    })
    d["total_hours"] = round(sum(s.get("hours", 0) for s in d["sessions"]), 2)
    with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)

def send_telegram(msg):
    pass

# Universal Clipboard Sync (64-bit Safe Win32 with Lock Retries)
def get_clipboard_text():
    try:
        import ctypes, time
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.02)
        if not opened:
            return ""

        CF_UNICODETEXT = 13
        h_data = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_data:
            user32.CloseClipboard()
            return ""
        p_data = kernel32.GlobalLock(h_data)
        text = ctypes.c_wchar_p(p_data).value or ""
        kernel32.GlobalUnlock(h_data)
        user32.CloseClipboard()
        return text
    except Exception as e:
        print(f"[CLIPBOARD GET ERR] {e}")
        return ""

def clear_clipboard():
    try:
        import ctypes, time
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.02)
        if not opened:
            return False
        user32.EmptyClipboard()
        user32.CloseClipboard()
        return True
    except Exception as e:
        print(f"[CLIPBOARD CLEAR ERR] {e}")
        return False

def set_clipboard_text(text):
    if not text:
        return clear_clipboard()
    try:
        import ctypes, time
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.02)
        if not opened:
            return False

        user32.EmptyClipboard()
        CF_UNICODETEXT = 13
        data = text.encode("utf-16-le") + b"\x00\x00"
        h_mem = kernel32.GlobalAlloc(0x0042, len(data))
        p_mem = kernel32.GlobalLock(h_mem)
        ctypes.memmove(p_mem, data, len(data))
        kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        user32.CloseClipboard()
        return True
    except Exception as e:
        print(f"[CLIPBOARD SET ERR] {e}")
        return False

# System Inactivity Tracker
def get_system_idle_seconds():
    try:
        import ctypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return millis / 1000.0
    except Exception:
        pass
    return 0.0

# Network Wi-Fi Devices
def get_network_devices():
    devices = []
    try:
        out = subprocess.check_output("arp -a", shell=True).decode("cp1252", errors="ignore")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0].startswith("192.168.1."):
                ip = parts[0]
                mac = parts[1]
                kind = parts[2]
                if mac != "ff-ff-ff-ff-ff-ff" and not ip.endswith(".255"):
                    name = "Gateway/Router" if ip == "192.168.1.1" else ("Zorin PC" if ip == PC_IP else ("Your Laptop" if "192.168.1.6" in ip else "Home Device"))
                    devices.append({"ip": ip, "mac": mac, "name": name, "type": kind})
    except Exception as e:
        print(f"[ARP SCAN ERR] {e}")
    return devices

# Full Diagnostics
def run_full_diagnostics():
    results = {}
    try:
        p = subprocess.run(["ping", "-n", "1", "-w", "1000", "8.8.8.8"], capture_output=True, text=True)
        ping_ms = "Offline"
        for line in p.stdout.splitlines():
            if "time=" in line or "time<" in line:
                ping_ms = line.split("time=")[-1].split("ms")[0] + "ms" if "time=" in line else "<1ms"
        results["internet"] = {"status": "Online" if p.returncode == 0 else "Disconnected", "latency": ping_ms}
    except:
        results["internet"] = {"status": "Error", "latency": "--"}

    results["laptop"] = get_laptop_stats()
    results["battery"] = get_battery()
    try:
        results["pc"] = {"online": True, "stats": get_pc_stats()}
    except:
        results["pc"] = {"online": False}

    with ac_state_lock:
        results["ac"] = {"connected": ac_connected, "state": ac_state.copy()}

    try:
        t0 = time.time()
        call_gemini("ping")
        results["ai"] = {"status": "Online (Gemini 3.6 Flash)", "latency": f"{round((time.time()-t0)*1000)}ms"}
    except Exception as e:
        results["ai"] = {"status": f"Degraded: {e}", "latency": "--"}

    return results

def extract_gemini_text(r):
    """Safely extracts text from Gemini response without throwing AttributeError on None."""
    if not r:
        return ""
    if hasattr(r, "text") and r.text:
        return r.text.strip()
    if hasattr(r, "candidates") and r.candidates:
        parts = []
        for c in r.candidates:
            if hasattr(c, "content") and c.content and hasattr(c.content, "parts"):
                for p in c.content.parts:
                    if hasattr(p, "text") and p.text:
                        parts.append(p.text)
        if parts:
            return "\n".join(parts).strip()
    return "Gemini AI analyzed the problem, but produced an empty response. Please re-take the photo with clear lighting and steady focus."

# Visual OCR & Screen Problem Solver
def solve_screen_problem():
    try:
        img_b64 = capture_laptop_screenshot()
        if "," in img_b64:
            img_bytes = base64.b64decode(img_b64.split(",")[1])
        else:
            img_bytes = base64.b64decode(img_b64)

        # Optimize with PIL to max 1280px JPEG to ensure lightning fast upload & analysis
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(img_bytes))
            if im.mode != "RGB":
                im = im.convert("RGB")
            if max(im.size) > 1280:
                im.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            img_bytes = buf.getvalue()
        except Exception as e_pil:
            print(f"[PIL SCREEN] {e_pil}")

        from google.genai import types
        prompt = (
            "You are a master academic tutor, scientist, and engineer. Look at this screen carefully.\n"
            "1. Identify the main problem, question, chemical structure, equation, or code error visible.\n"
            "2. Provide the clear, complete, step-by-step mathematical or conceptual explanation.\n"
            "3. State the final answer clearly in bold.\n"
            "Format the response in clean, beautiful Markdown."
        )
        r = call_gemini([
            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
            prompt
        ])
        return extract_gemini_text(r)
    except Exception as e:
        return f"Screen problem solving failed: {e}"

# Mobile / Uploaded Photo Problem Solver
def solve_uploaded_image(img_data):
    try:
        if isinstance(img_data, str):
            if "data:" in img_data and ";base64," in img_data:
                b64_part = img_data.split(";base64,")[1]
            elif "," in img_data:
                b64_part = img_data.split(",")[1]
            else:
                b64_part = img_data
            img_bytes = base64.b64decode(b64_part)
        else:
            img_bytes = img_data

        def _detect_mime(b):
            if b.startswith(b"\xff\xd8\xff"):
                return "image/jpeg"
            if b.startswith(b"\x89PNG\r\n\x1a\n"):
                return "image/png"
            if b.startswith(b"RIFF") and len(b) > 12 and b[8:12] == b"WEBP":
                return "image/webp"
            if len(b) > 12 and b[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1", b"ftypmsf1"):
                return "image/heic"
            return "image/jpeg"

        mime = _detect_mime(img_bytes)

        # Auto-rescale & convert to standard JPEG via PIL if possible
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(img_bytes))
            if im.mode != "RGB":
                im = im.convert("RGB")
            if max(im.size) > 1280:
                im.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            img_bytes = buf.getvalue()
            mime = "image/jpeg"
        except Exception as e_pil:
            print(f"[PIL OPTIMIZE] {e_pil}")

        from google.genai import types
        prompt = (
            "You are a master academic tutor, scientist, and engineer. Look at this uploaded question or study image carefully.\n"
            "1. Read and transcribe the main question, equation, or problem shown.\n"
            "2. Provide the clear, complete, step-by-step mathematical or conceptual explanation.\n"
            "3. State the final answer clearly in bold.\n"
            "Format the response in clean, beautiful Markdown."
        )
        r = call_gemini([
            types.Part.from_bytes(data=img_bytes, mime_type=mime),
            prompt
        ])
        return extract_gemini_text(r)
    except Exception as e:
        print(f"[PHOTO SOLVE ERR] {e}")
        return f"Photo problem solving failed: {e}"

# YouTube Playback Automation
def play_youtube(query, device="laptop", return_dict=False):
    import urllib.parse
    import urllib.request
    q = query.strip()
    if not q:
        q = "lofi hip hop radio - beats to relax/study to"

    video_id = ""
    video_url = ""

    # 1. Direct YouTube link or ID detection
    m = re.search(r"(?:v=|\/|embed\/|shorts\/)([a-zA-Z0-9_-]{11})", q)
    if m:
        video_id = m.group(1)
        video_url = f"https://www.youtube.com/watch?v={video_id}"
    else:
        search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(q)}"
        video_url = search_url
        try:
            req = urllib.request.Request(
                search_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            )
            html = urllib.request.urlopen(req, timeout=5).read().decode("utf-8", errors="ignore")
            vids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
            if not vids:
                vids = re.findall(r'watch\?v=([a-zA-Z0-9_-]{11})', html)
            if vids:
                video_id = vids[0]
                video_url = f"https://www.youtube.com/watch?v={video_id}"
        except Exception as ex:
            print(f"[YT SCRAPE] {ex}")

    video_watch_url = f"https://www.youtube.com/watch?v={video_id}&autoplay=1" if video_id else video_url

    if device == "pc":
        execute_on_pc(f"nohup brave-browser --autoplay-policy=no-user-gesture-required '{video_watch_url}' > /tmp/yt.log 2>&1 &")
        msg = f"Playing '{q}' on Zorin PC YouTube."
    elif device == "device":
        msg = f"Playing '{q}' on this device."
    else:
        open_url(video_watch_url)
        msg = f"Playing '{q}' on Laptop YouTube."

    res = {
        "result": msg,
        "url": video_url,
        "video_id": video_id,
        "query": q,
        "device": device
    }
    return res if return_dict else msg

# Local Study Material & File Indexer
SEARCH_DIRS = [
    os.path.join(os.path.expanduser("~"), "Desktop"),
    os.path.join(os.path.expanduser("~"), "Downloads"),
    os.path.join(os.path.expanduser("~"), "Documents"),
]

def search_local_files(query, max_results=20):
    q = query.lower().strip()
    results = []
    if not q:
        return results
    ext_icons = {
        ".pdf": "📕", ".doc": "📘", ".docx": "📘", ".ppt": "📙", ".pptx": "📙",
        ".xls": "📗", ".xlsx": "📗", ".txt": "📄", ".py": "🐍", ".png": "🖼️",
        ".jpg": "🖼️", ".jpeg": "🖼️", ".mp4": "🎥", ".mp3": "🎵", ".zip": "📦", ".bat": "⚙️"
    }
    for base in SEARCH_DIRS:
        if not os.path.exists(base):
            continue
        try:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() not in ['node_modules', 'appdata', '__pycache__', 'temp']]
                for f in files:
                    if q in f.lower():
                        fp = os.path.join(root, f)
                        try:
                            st = os.stat(fp)
                            ext = os.path.splitext(f)[1].lower()
                            size_mb = round(st.st_size / (1024 * 1024), 2)
                            size_str = f"{size_mb} MB" if size_mb >= 1 else f"{round(st.st_size / 1024, 1)} KB"
                            mod_str = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%b %d, %Y")
                            results.append({
                                "name": f,
                                "path": fp,
                                "size": size_str,
                                "modified": mod_str,
                                "icon": ext_icons.get(ext, "📁"),
                                "ext": ext
                            })
                            if len(results) >= max_results:
                                return results
                        except Exception:
                            pass
        except Exception:
            pass
    return results

def open_local_file(filepath):
    try:
        if os.path.exists(filepath):
            subprocess.Popen(["cmd", "/c", "start", "", filepath], shell=False)
            return f"Opened: {os.path.basename(filepath)}"
        return "File not found."
    except Exception as e:
        return f"Failed to open file: {e}"

# Focus Mode Distraction Guard
def check_focus_distractions():
    try:
        img_b64 = capture_laptop_screenshot()
        img_bytes = base64.b64decode(img_b64.split(",")[1])
        from google.genai import types
        prompt = (
            "Analyze if this computer screen shows productive study/work (e.g. Physics Wallah, coding, documents, notes) "
            "OR if it is distracted by gaming, entertainment, social media, or off-task browsing. "
            "Reply with 'STUDYING' if focused, or 'DISTRACTED: <specific reason>' if off-task."
        )
        r = call_gemini([
            types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
            prompt
        ])
        res = r.text.strip()
        if "DISTRACTED" in res.upper():
            reason = res.split(":")[-1].strip() if ":" in res else "off-task browsing"
            announce_via_speakers("Heisenberg, gentle reminder: you are in focus mode. Let's get back to studying.")
            return f"Distraction detected ({reason}). Voice reminder given."
        return "Focus verified: screen shows active productive work. Keep it up!"
    except Exception as e:
        return f"Distraction check failed: {e}"

# Active Recall Flashcards
def generate_flashcards(topic):
    try:
        prompt = (
            f"Generate 5 high-yield active-recall study flashcards on the topic: '{topic}'. "
            "Output ONLY valid JSON array with objects having 'q' (question) and 'a' (clear, concise answer). "
            'Format: [{"q": "Question?", "a": "Answer"}, ...]'
        )
        r = call_gemini(prompt)
        raw = re.sub(r"```json|```", "", r.text).strip()
        return json.loads(raw)
    except Exception as e:
        return [{"q": f"Topic: {topic}", "a": f"Flashcard generation failed: {e}"}]

# Automated Window Layout Splitter
def snap_study_windows():
    try:
        import ctypes
        user32 = ctypes.windll.user32
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        half_w = sw // 2
        windows = []
        def enum_cb(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    windows.append((hwnd, buff.value))
            return True
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        browsers = [w for w in windows if any(b in w[1].lower() for b in ["chrome", "brave", "edge", "physics wallah", "pw", "molview", "gemini"])]
        if len(browsers) >= 1:
            user32.ShowWindow(browsers[0][0], 9)
            user32.MoveWindow(browsers[0][0], 0, 0, half_w, sh, True)
        if len(browsers) >= 2:
            user32.ShowWindow(browsers[1][0], 9)
            user32.MoveWindow(browsers[1][0], half_w, 0, half_w, sh, True)
        return "Study windows arranged in split-screen layout."
    except Exception as e:
        return f"Window snap error: {e}"

# Autonomous Background Daemon (Battery Guard, Weather Auto-AC, Idle Sleep, High-CPU)
_auto_state = {
    "bat_low": False,
    "bat_high": False,
    "last_idle_sleep": 0,
    "last_wx_check": 0,
    "last_high_cpu": 0,
}

def _automation_daemon():
    print("[AUTONOMOUS] Background OS Guards & Automations active...")
    while True:
        try:
            now = datetime.datetime.now()
            
            # 1. Battery Voice Guard (15% & 80% rules)
            b = get_battery()
            pct = b.get("percent", -1)
            plugged = b.get("plugged", False)
            if pct > 0:
                if not plugged and pct <= 15 and not _auto_state["bat_low"]:
                    announce_via_speakers(f"Warning Heisenberg: Battery is critically low at {pct} percent. Please connect your charger.")
                    _auto_state["bat_low"] = True
                elif pct > 20:
                    _auto_state["bat_low"] = False

                if plugged and pct >= 80 and not _auto_state["bat_high"]:
                    announce_via_speakers(f"Heisenberg: Battery has reached {pct} percent. You can disconnect charger to protect battery health.")
                    _auto_state["bat_high"] = True
                elif pct < 75:
                    _auto_state["bat_high"] = False

            # 2. Inactivity Sleep Trigger (After 1:00 AM)
            if 1 <= now.hour < 5:
                idle = get_system_idle_seconds()
                if idle > 900 and (time.time() - _auto_state["last_idle_sleep"] > 1800):
                    _auto_state["last_idle_sleep"] = time.time()
                    print("[IDLE SLEEP] Inactivity detected past 1 AM. Engaging sleep mode...")
                    announce_via_speakers("No activity detected after 1 AM. Activating sleep mode.")
                    execute_scene("sleep mode")

            # 3. Weather & AQI Auto-AC Rule (Every 10 mins)
            if time.time() - _auto_state["last_wx_check"] > 600:
                _auto_state["last_wx_check"] = time.time()
                wx = get_weather()
                otemp = wx.get("temp", 0)
                if otemp >= 33:
                    with ac_state_lock:
                        if ac_state.get("power") == "on" and ac_state.get("temp", 24) > 22:
                            print(f"[AUTO AC] Outdoor temp {otemp}C >= 33C. Cooling to 22C.")
                            execute_ac_command("temp", 22)
                            announce_via_speakers(f"High outdoor heat detected at {round(otemp)} degrees. Auto-adjusting AC to 22 degrees.")

            # 4. High-CPU Guard
            cpu_now = psutil.cpu_percent(interval=None)
            if cpu_now > 90 and (time.time() - _auto_state["last_high_cpu"] > 120):
                procs = []
                for p in psutil.process_iter(['name', 'cpu_percent']):
                    try:
                        name = p.info['name'] or ""
                        if "idle" not in name.lower():
                            procs.append((name, p.info['cpu_percent']))
                    except:
                        pass
                procs.sort(key=lambda x: x[1] or 0, reverse=True)
                top = procs[0][0] if procs else "None"
                _auto_state["last_high_cpu"] = time.time()
                if top != "None":
                    print(f"[HIGH CPU] CPU at {cpu_now}% - Top consumer: {top}")
        except Exception:
            pass
        time.sleep(30)

threading.Thread(target=_automation_daemon, daemon=True).start()


# Announce via speakers (Windows TTS)
def announce_via_speakers(text):
    safe = text.replace('"',"'")
    run_laptop(f'powershell -c "Add-Type -AssemblyName System.speech; (New-Object Speech.Synthesis.SpeechSynthesizer).Speak(\\"{safe}\\")"',shell=True)
    return f'Announced: "{text}"'

# Speedtest (runs on PC via SSH)
def run_speedtest():
    r=execute_on_pc("command -v speedtest-cli > /dev/null && speedtest-cli --simple || echo 'Install: pip install speedtest-cli on PC'")
    return r or "Speedtest complete."

# Night mode
def night_mode():
    return execute_scene("sleep mode")

# Screenshot → base64 (Fast & DPI-Aware via PIL + Desktop Attachment)
def capture_laptop_screenshot():
    sp = os.path.join(SCREENSHOT_DIR, "latest.png")
    try:
        import ctypes
        from PIL import ImageGrab
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
            user32.CloseDesktop(hdesk)
        img = ImageGrab.grab(all_screens=True)
        img.save(sp, "PNG")
        with open(sp, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except Exception as e:
        print(f"[SCREENSHOT FALLBACK] {e}")
        ps1 = os.path.join(SCREENSHOT_DIR, "capture.ps1")
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", ps1], timeout=10, capture_output=True)
        with open(sp, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()

def capture_pc_screenshot():
    execute_on_pc("scrot /tmp/jscreen.png"); time.sleep(1.5)
    ssh=paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(PC_IP,username=PC_USER,password=PC_PASS,timeout=5)
    sftp=ssh.open_sftp(); lp=os.path.join(SCREENSHOT_DIR,"pc_latest.png")
    sftp.get("/tmp/jscreen.png",lp); sftp.close(); ssh.close()
    with open(lp,"rb") as f: return "data:image/png;base64,"+base64.b64encode(f.read()).decode()

# Gemini screen describe
def describe_screen(device):
    try:
        img_b64 = capture_laptop_screenshot() if device=="laptop" else capture_pc_screenshot()
        img_bytes = base64.b64decode(img_b64.split(",")[1])
        from google.genai import types
        r = call_gemini([
            types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
            "Analyze and describe what is currently open on this screen. Mention visible applications, documents, errors, or content clearly in 2 to 3 sentences."
        ])
        return r.text.strip()
    except Exception as e: return f"Screen description failed: {e}"

# ==========================================
# REMINDER SYSTEM
# ==========================================
def _rem_watch():
    while True:
        now=datetime.datetime.now()
        with reminders_lock:
            due=[r for r in reminders if now>=r["time"]]
            for r in due:
                print(f"\n[REMINDER] {r['text']}\n")
                announce_via_speakers(f"Reminder: {r['text']}")
                if TELEGRAM_TOKEN: send_telegram(f"REMINDER: {r['text']}")
                reminders.remove(r)
        time.sleep(20)

threading.Thread(target=_rem_watch,daemon=True).start()

def add_reminder(text,minutes):
    global _rid
    at=datetime.datetime.now()+datetime.timedelta(minutes=int(minutes))
    with reminders_lock:
        reminders.append({"id":_rid,"text":text,"time":at}); _rid+=1
    return f'Reminder set for {minutes} min: "{text}"'

# Wikipedia — full rich search
def wiki_search(q):
    q = q.strip()
    if not q:
        return "Please enter a search term."
    wikipedia.set_lang("en")
    try:
        candidates = wikipedia.search(q, results=5, suggestion=True)
        titles = candidates[0] if isinstance(candidates, tuple) else candidates
        if not titles:
            return f'No Wikipedia results for "{q}".'
        last_err = ""
        for title in titles:
            try:
                page = wikipedia.page(title, auto_suggest=False, redirect=True, preload=False)
                raw = page.summary
                sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', raw) if s.strip()]
                summary = " ".join(sentences[:10])
                cats = ", ".join(page.categories[:5]) if page.categories else "N/A"
                return f"TITLE:{page.title}\nSUMMARY:{summary}\nURL:{page.url}\nCATS:{cats}"
            except wikipedia.exceptions.DisambiguationError as e:
                for opt in e.options[:5]:
                    try:
                        page = wikipedia.page(opt, auto_suggest=False, redirect=True)
                        raw = page.summary
                        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', raw) if s.strip()]
                        summary = " ".join(sentences[:10])
                        return f"TITLE:{page.title}\nSUMMARY:{summary}\nURL:{page.url}\nCATS:N/A"
                    except Exception:
                        continue
                last_err = f'Disambiguation: {", ".join(e.options[:5])}'
            except wikipedia.exceptions.PageError:
                last_err = f'Page not found: {title}'; continue
            except Exception as ex:
                last_err = str(ex); continue
        return f'Could not load Wikipedia for "{q}". ({last_err})'
    except Exception as e:
        return f"Wikipedia error: {e}"

# ==========================================
# COMMAND PROCESSOR
# ==========================================
def process_command(raw):
    cmd = raw.lower().strip()
    with history_lock:
        command_history.append({"cmd": raw, "time": datetime.datetime.now().strftime("%H:%M:%S")})

    # Clean wake words at start
    cleaned_wake = re.sub(r"^(hey\s+jarvis|jarvis|ok\s+jarvis|please|can\s+you\s+please|can\s+you|could\s+you\s+please|could\s+you)\s*,?\s*", "", cmd, flags=re.IGNORECASE).strip()
    if cleaned_wake:
        cmd = cleaned_wake

    # ── 0. MASTER OPEN / LAUNCH ROUTER ──
    # Handles "open youtube on laptop", "open brave in laptop", "open pw on laptop", "open calculator", etc.
    if re.search(r"\b(open|launch|start)\b", cmd, re.IGNORECASE) and not cmd.startswith("open file") and not cmd.startswith("open document"):
        dev, target = extract_device_and_clean(cmd)
        if target:
            if target in WEBSITES or target == "youtube" or any(target.endswith(suf) for suf in [".com", ".org", ".net", ".io", ".live"]):
                url = WEBSITES.get(target, target if target.startswith("http") else "https://" + target)
                if dev == "pc":
                    execute_on_pc(f"nohup brave-browser '{url}' > /tmp/j.log 2>&1 &")
                else:
                    if sys.platform != "win32":
                        laptop_ip = os.getenv("LAPTOP_IP", "192.168.1.6")
                        if http_req:
                            http_req.post(f"http://{laptop_ip}:5000/run", json={"command": f"open {target} on laptop"}, timeout=5)
                    else:
                        open_url(url)
                return f"Opened {target} on {dev}."
            else:
                execute_action("open_app", dev, target)
                return f"Opened {target} on {dev}."

    # ── 0. MASTER CLOSE / KILL ROUTER ──
    # Handles "close brave on laptop", "close brave in pc", "kill spotify", etc.
    if re.search(r"\b(close|kill|terminate|exit|quit)\b", cmd, re.IGNORECASE) and not any(k in cmd for k in ["music", "song", "audio", "playback", "video"]):
        dev, target = extract_device_and_clean(cmd)
        if target:
            execute_action("close_app", dev, target)
            return f"Closed {target} on {dev}."


    # ── Study session check MUST come before AC check ──────────────────────────
    # Reason: "activate study session on laptop" contains "ac" (in "activate")
    # and "on" (in "on laptop") which would false-match the AC block below.
    if "study" in cmd and "session" in cmd:
        dev = "laptop" if "laptop" in cmd else "pc"
        print(f"\n[STUDY] Starting study session on {dev}")

        def _run_session():
            # Open all 4 study URLs
            urls = [
                ("PW Live",      WEBSITES["pw"]),
                ("MolView 3D",   WEBSITES["chemistry 3d"]),
                ("Gemini",       WEBSITES["gemini"]),
                ("ChatGPT",      WEBSITES["chatgpt"]),
            ]
            if dev == "pc":
                for name, u in urls:
                    print(f"[STUDY] Opening {name} on PC: {u}")
                    execute_on_pc(f"nohup xdg-open {u} > /tmp/j.log 2>&1 &")
                    time.sleep(1.2)
            else:
                for i, (name, u) in enumerate(urls):
                    print(f"[STUDY] Opening {name} on laptop: {u}")
                    open_url(u)
                    time.sleep(1.5 if i == 0 else 1.0)

            # Turn on AC to 24°C
            print("[STUDY] Turning AC on at 24C")
            execute_ac_command("power", "on")
            execute_ac_command("temp", 24)
            print("[STUDY] Session started successfully!")

            # 2-hour completion callback
            time.sleep(7200)
            execute_ac_command("power", "off")
            log_study_session(2.0, dev)
            announce_via_speakers("Study session complete! Great work. AC turned off.")
            if TELEGRAM_TOKEN:
                send_telegram("Study session done! 2 hours complete. AC off.")

        threading.Thread(target=_run_session, daemon=True).start()
        return f"Study session starting on {dev}! Opening PW, MolView, Gemini, ChatGPT... AC will turn on to 24C. Watch your browser."


    # ── Scene Modes ───────────────────────────────────────────
    for sk in ["movie night", "gaming mode", "focus mode", "sleep mode", "morning routine"]:
        if sk in cmd or (sk.replace(" mode", "") in cmd and any(w in cmd for w in ["activate", "turn on", "start", "enable", "mode"])):
            return execute_scene(sk)

    if any(k in cmd for k in ["daily briefing", "morning briefing", "briefing", "morning report", "daily report"]):
        return get_daily_briefing()

    if any(k in cmd for k in ["news", "headlines", "what's happening in india", "top stories"]):
        items = get_news()
        if not items:
            return "No news headlines available right now."
        lines = [f"{i+1}. {x['title']}" for i, x in enumerate(items[:5])]
        return "📰 Top Headlines:\n" + "\n".join(lines)

    # ── AC / climate control ──────────────────────────────────────────────────
    # Use word-boundary check: " ac ", "the ac", "my ac" or cmd starts with "ac"
    # This avoids false-matching "activate", "replace", etc.
    def _is_ac_cmd(c):
        return (c.startswith("ac ") or c == "ac" or
                " ac " in c or c.endswith(" ac") or
                "air conditioner" in c or "climate" in c)

    if _is_ac_cmd(cmd):
        if any(k in cmd for k in ["on","turn on","start"]): return execute_ac_command("power","on")
        if any(k in cmd for k in ["off","turn off","stop"]): return execute_ac_command("power","off")
        if any(k in cmd for k in ["set","temp","degree"]):
            n=re.findall(r"\d+",cmd)
            return execute_ac_command("temp",n[0]) if n else "Specify temperature."
        if any(k in cmd for k in ["increase","hotter","warmer"]):
            with ac_state_lock: nv=ac_state["temp"]+1
            return execute_ac_command("temp",nv)
        if any(k in cmd for k in ["decrease","cooler","colder"]):
            with ac_state_lock: nv=ac_state["temp"]-1
            return execute_ac_command("temp",nv)
        return "AC command not understood."

    if any(k in cmd for k in ["goodnight","good night","night mode"]): return night_mode()

    if any(k in cmd for k in ["describe screen","describe my screen","what's on screen","analyze screen","what am i looking"]):
        return describe_screen("laptop" if "laptop" in cmd else "pc")

    if any(k in cmd for k in ["solve screen","solve problem","homework help","solve this","solution on screen","explain problem"]):
        return solve_screen_problem()

    if any(k in cmd for k in ["check distraction","distraction check","distraction guard","am i focused","check focus","focus check"]):
        return check_focus_distractions()

    if "flashcard" in cmd:
        topic = re.sub(r"(generate flashcards|create flashcards|make flashcards|flashcards on|flashcard on|flashcards|flashcard)", "", cmd).strip() or "general science"
        cards = generate_flashcards(topic)
        if isinstance(cards, list):
            out = [f"📚 Flashcards: {topic.title()}"]
            for i, c in enumerate(cards[:5], 1):
                out.append(f"Q{i}: {c.get('q','')}\nA{i}: {c.get('a','')}")
            return "\n\n".join(out)
        return str(cards)

    if any(k in cmd for k in ["snap windows","split screen","split study","arrange windows","snap study"]):
        return snap_study_windows()

    if cmd in ["clear clipboard", "empty clipboard"]:
        if clear_clipboard():
            return "Laptop clipboard cleared."
        return "Failed to clear clipboard."

    if cmd in ["clipboard","get clipboard","show clipboard","read clipboard","what is on clipboard"]:
        cb = get_clipboard_text()
        return f"📋 Laptop Clipboard:\n{cb}" if cb else "Clipboard is empty."

    if cmd.startswith("set clipboard ") or cmd.startswith("copy to laptop "):
        val = re.sub(r"^(set clipboard|copy to laptop)\s+", "", raw, flags=re.IGNORECASE).strip()
        if set_clipboard_text(val):
            return f"Copied to laptop clipboard: {val[:60]}..."
        return "Failed to set clipboard."

    # ── Media Playback & Controls ────────────────────────────
    # 1. Play/Pause Toggle (intercepts UI play/pause buttons before YouTube search)
    if any(k in cmd for k in ["play pause", "play/pause", "toggle play", "toggle music", "pause/play", "play/stop"]):
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("media_play_pause", dev)
        return f"Toggled media play/pause on {dev}."

    # 2. Stop & Silence Media (guaranteed stop for any song / runaway audio)
    if any(k in cmd for k in ["stop music", "stop song", "stop the song", "stop video", "stop playing", "kill music", "be quiet", "shut up", "silence", "stop playback"]) or cmd == "stop":
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("media_play_pause", dev)
        if dev == "laptop":
            try:
                import subprocess
                subprocess.run("taskkill /f /im brave.exe", shell=True, capture_output=True)
            except Exception:
                pass
        return f"Stopped and silenced all media on {dev}."

    # 3. Pause
    if any(k in cmd for k in ["pause music", "pause song", "pause video", "pause playback"]) or cmd == "pause":
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("media_play_pause", dev)
        return f"Paused media playback on {dev}."

    # 4. Resume / Unpause
    if any(k in cmd for k in ["resume music", "resume song", "resume video", "unpause", "continue music", "keep playing", "resume"]):
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("media_play_pause", dev)
        return f"Resumed media playback on {dev}."

    # 5. Next Track & Skip
    if any(k in cmd for k in ["next song", "next track", "next video", "skip song", "skip track"]) or cmd in ["next", "skip"]:
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("media_next", dev)
        return f"Playing next track on {dev}."

    # 6. Previous Track
    if any(k in cmd for k in ["previous song", "prev song", "previous track", "prev track", "restart song", "replay song", "last song"]) or cmd in ["prev", "previous"]:
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("media_prev", dev)
        return f"Playing previous track on {dev}."

    # 7. Volume Up
    if any(k in cmd for k in ["volume up", "louder", "turn it up", "increase volume", "raise volume"]):
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("volume_up", dev)
        return f"Volume up on {dev}."

    # 8. Volume Down
    if any(k in cmd for k in ["volume down", "quieter", "turn it down", "decrease volume", "lower volume"]):
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("volume_down", dev)
        return f"Volume down on {dev}."

    # 9. Mute
    if any(k in cmd for k in ["mute", "unmute"]):
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("volume_mute", dev)
        return f"Toggled mute on {dev}."

    # 10. Fullscreen
    if any(k in cmd for k in ["fullscreen", "full screen", "maximize video"]):
        focus_browser_window()
        send_key(0x46) # 'f' key toggles YouTube fullscreen
        return "Toggled fullscreen video."

    # 11. YouTube Search & Playback
    if "youtube" in cmd or any(cmd.startswith(p) for p in ["play ", "listen to ", "put on ", "stream ", "watch "]) or cmd in ["play", "play music", "play song", "play something"]:
        q = re.sub(r"^(play on youtube|play|listen to|put on|stream|watch on youtube|watch|youtube search|youtube)\s+", "", raw, flags=re.IGNORECASE).strip()
        dev = "pc" if "on pc" in cmd else "laptop"
        q = re.sub(r"\s+(on pc|on laptop)$", "", q, flags=re.IGNORECASE).strip()
        if not q or q.lower() in ["music", "song", "a song", "songs", "something", "anything"]:
            q = "lofi hip hop radio - beats to relax/study to"
        return play_youtube(q, dev, return_dict=True)

    # ── Local File Search & Opener ────────────────────────────
    if any(k in cmd for k in ["find file", "search file", "search for file", "find my", "search document", "search downloads"]):
        q = re.sub(r"(find file|search file|search for file|find my|search document|search downloads|file|document)", "", raw, flags=re.IGNORECASE).strip()
        matches = search_local_files(q)
        if not matches:
            return f"No local files found matching '{q}'."
        lines = [f"{m['icon']} {m['name']} ({m['size']}) - {m['path']}" for m in matches[:5]]
        return f"Found {len(matches)} files matching '{q}':\n" + "\n".join(lines)

    if cmd.startswith("open file ") or cmd.startswith("open document "):
        path = re.sub(r"^(open file|open document)\s+", "", raw, flags=re.IGNORECASE).strip().strip('"').strip("'")
        return open_local_file(path)

    # ── Study Targets & Goal Tracker ──────────────────────────
    if any(k in cmd for k in ["add goal", "add study target", "new goal", "add target", "set goal"]):
        t = re.sub(r"(add goal|add study target|new goal|add target|set goal|goal:)", "", raw, flags=re.IGNORECASE).strip()
        if t:
            add_goal(t)
            return f"Study target added: '{t}'"
        return "What study target should I add?"

    if cmd in ["goals", "study targets", "my goals", "show goals", "today's goals", "targets"]:
        gd = load_goals()
        goals = gd.get("goals", [])
        if not goals:
            return "No study targets set for today. Add one by saying 'add goal: ...'"
        lines = [f"{'✅' if g.get('done') else '⭕'} [{g.get('subject','Study')}] {g['text']}" for g in goals]
        return f"🎯 Study Targets (Streak: {gd.get('streak',1)} days):\n" + "\n".join(lines)

    # ── Zorin Linux PC Terminal ───────────────────────────────
    if cmd.startswith("run on pc ") or cmd.startswith("terminal pc ") or cmd.startswith("bash on pc "):
        c = re.sub(r"^(run on pc|terminal pc|bash on pc)\s+", "", raw, flags=re.IGNORECASE).strip()
        res = execute_pc_terminal(c)
        out = res.get("output") or res.get("error") or "Done (no output)."
        return f"🖥️ Zorin PC ($ {c}):\n{out}"

    if any(k in cmd for k in ["network devices","wifi devices","who is on wifi","connected devices","scan network"]):
        devs = get_network_devices()
        if not devs:
            return "No Wi-Fi devices detected in ARP table."
        lines = [f"• {d['name']} ({d['ip']}) - {d['mac']}" for d in devs]
        return f"📶 Connected Network Devices ({len(devs)} found):\n" + "\n".join(lines)

    if any(k in cmd for k in ["diagnostics","system health","health check","system audit","network audit","system test"]):
        diag = run_full_diagnostics()
        lines = [
            f"Internet: {diag.get('internet',{}).get('status','--')} ({diag.get('internet',{}).get('latency','--')})",
            f"Laptop: CPU {diag.get('laptop',{}).get('cpu','--')}%, RAM {diag.get('laptop',{}).get('ram','--')}%, Battery {diag.get('battery',{}).get('percent','--')}%",
            f"Zorin PC: {'Online' if diag.get('pc',{}).get('online') else 'Offline'}",
            f"AC: {'Connected' if diag.get('ac',{}).get('connected') else 'Disconnected'} (Temp: {diag.get('ac',{}).get('state',{}).get('temp','--')}°C)",
            f"AI: {diag.get('ai',{}).get('status','--')} ({diag.get('ai',{}).get('latency','--')})"
        ]
        return "🛠️ System Diagnostics:\n" + "\n".join(lines)

    if "telegram" in cmd and any(k in cmd for k in ["send","message","notify"]):
        msg=re.sub(r"(send telegram|send message|telegram|notify me|send)","",cmd).strip()
        return send_telegram(msg or "Hello from Jarvis!")

    if any(k in cmd for k in ["announce","say out loud","speak aloud"]):
        t=re.sub(r"(announce|say out loud|speak aloud)","",cmd).strip()
        return announce_via_speakers(t) if t else "What should I announce?"

    if any(k in cmd for k in ["speedtest","internet speed","network speed","speed test"]):
        return run_speedtest()

    if any(k in cmd for k in ["save note","take note","note down","add note","note that"]):
        t=re.sub(r"(save note|take note|note down|add note|note that|note:)","",cmd).strip()
        return add_note(t) if t else "What should I note?"

    if "wikipedia" in cmd or ("wiki" in cmd and "search" not in cmd):
        q=re.sub(r"(wikipedia|wiki|look up|define|what is|search for)","",cmd).strip()
        return wiki_search(q)

    if any(k in cmd for k in ["remind me","reminder","alert me"]):
        n=re.findall(r"\d+",cmd); mins=n[0] if n else "30"
        t=re.sub(r"(remind me|reminder|alert me|in \d+ minutes?|after \d+ minutes?)","",cmd).strip() or "Reminder!"
        return add_reminder(t,mins)



    if "search" in cmd or ("google" in cmd and "open" not in cmd):
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        q = re.sub(r"(search for|search|google|on my laptop|on my pc|on laptop|on pc)", "", cmd).strip()
        url = f"https://www.google.com/search?q={urllib.parse.quote(q)}"
        if dev == "pc": execute_on_pc(f"nohup xdg-open '{url}' > /tmp/j.log 2>&1 &")
        else: open_url(url)
        return f'Searching "{q}" on {dev}.'

    if "open" in cmd and any(s in cmd for s in WEBSITES):
        site = next(s for s in WEBSITES if s in cmd); url = WEBSITES[site]
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        if dev == "pc":
            br = "brave-browser" if "brave" in cmd else "msedge" if "edge" in cmd else "google-chrome-stable"
            execute_on_pc(f"nohup {br} {url} > /tmp/j.log 2>&1 &")
        else: open_url(url)
        return f"Opened {site} on {dev}."

    if "screenshot" in cmd or "capture screen" in cmd:
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("take_screenshot", dev); return f"Screenshot taken on {dev}."

    if "close" in cmd or ("kill" in cmd and "app" in cmd):
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        a = re.sub(r"(close|kill|on my laptop|on my pc|on laptop|on pc|the|app)", "", cmd).strip()
        execute_action("close_app", dev, a); return f"Closed {a} on {dev}."

    if "open" in cmd:
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        a = re.sub(r"(open|on my laptop|on my pc|on laptop|on pc|the|app)", "", cmd).strip()
        execute_action("open_app", dev, a); return f"Opened {a} on {dev}."

    if "shutdown" in cmd or ("turn off" in cmd and any(d in cmd for d in ["pc","laptop","computer"])):
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("shutdown_pc", dev); return f"Shutting down {dev}..."
    if "sleep" in cmd or "suspend" in cmd:
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        execute_action("sleep_pc", dev); return f"Putting {dev} to sleep..."

    if any(k in cmd for k in ["status", "stats", "performance", "how is"]):
        dev = "pc" if ("on pc" in cmd or " pc" in cmd) else "laptop"
        s = get_laptop_stats() if dev == "laptop" else get_pc_stats()
        return f"{dev.upper()} -- CPU:{s['cpu']}% RAM:{s['ram']}% Disk:{s['disk']}% Uptime:{s['uptime']}"

    if any(k in cmd for k in ["time","date","today","day is it"]):
        n=datetime.datetime.now()
        return f"It is {n.strftime('%A, %B %d %Y')} and the time is {n.strftime('%I:%M %p')}."

    if any(k in cmd for k in ["weather","temperature outside","forecast"]):
        w=get_weather()
        if "error" in w: return f"Weather unavailable: {w['error']}"
        return f"{w['icon']} {w['condition']}, {w['temp']}C. Humidity:{w['humidity']}%, Wind:{w['wind']}km/h."

    if any(k in cmd for k in ["help","what can you do","commands"]):
        return ("I can: control PC & laptop (apps, media, volume, shutdown, sleep, screenshot), "
                "manage AC, describe screen with AI, check weather, save notes, "
                "search Google/Wikipedia, open websites, set reminders (with voice+Telegram alerts), "
                "study sessions with analytics, speedtest, goodnight mode, and answer questions via Gemini AI.")

    with memory_lock:
        chat_memory.append(f"User: {cmd}"); mem="\n".join(chat_memory)
    try:
        resp = call_gemini(SYSTEM_PROMPT+f"\n\nHistory:\n{mem}\n\nJSON for: {cmd}")
        data=json.loads(re.sub(r"```json|```","",resp.text).strip())
        for item in data.get("actions",[]):
            a,d,t=item.get("action",""),item.get("device","laptop"),item.get("target","")
            if a=="ac_power": execute_ac_command("power",t)
            elif a=="ac_temp": execute_ac_command("temp",t)
            else: execute_action(a,d,t)
        reply=data.get("response_text","Done.")
        with memory_lock: chat_memory.append(f"Jarvis: {reply}")
        return reply
    except Exception as e:
        return f"Could not process. Try 'help'. ({e})"

# ==========================================
# FLASK ROUTES
# ==========================================
@app.route("/")
def index():
    try:
        with open(_tpl, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        content = HTML
    resp = app.make_response(content)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/run",methods=["POST"])
def run_cmd():
    d = request.json or {}
    c = d.get("command", "").strip()
    if not c:
        return jsonify({"result": "No command."})
    res = process_command(c)
    if isinstance(res, dict):
        return jsonify(res)
    return jsonify({"result": res})

@app.route("/news")
def news_r():
    return jsonify(get_news())

@app.route("/scenes")
def scenes_r():
    return jsonify(list(SCENES.keys()))

@app.route("/scene/<sname>", methods=["POST"])
def scene_run(sname):
    return jsonify({"result": execute_scene(sname)})

@app.route("/briefing")
def briefing_r():
    return jsonify({"result": get_daily_briefing()})

@app.route("/status")
def status():
    with ac_state_lock: ac=ac_state.copy()
    return jsonify({"laptop":get_laptop_stats(),"battery":get_battery(),
                    "ac":ac,"ac_connected":ac_connected,
                    "time":datetime.datetime.now().strftime("%I:%M %p"),
                    "date":datetime.datetime.now().strftime("%A, %B %d")})

@app.route("/weather")
def weather_r(): return jsonify(get_weather())

@app.route("/notes",methods=["GET"])
def notes_get(): return jsonify(load_notes())

@app.route("/notes",methods=["POST"])
def notes_post():
    d=request.json or {}; t=d.get("text","").strip()
    return jsonify({"result":add_note(t)}) if t else jsonify({"error":"No text"})

@app.route("/notes/<int:nid>",methods=["DELETE"])
def notes_del(nid): return jsonify({"result":delete_note(nid)})

@app.route("/analytics")
def analytics_r(): return jsonify(load_analytics())

@app.route("/history")
def history_r():
    with history_lock: return jsonify(list(command_history))

@app.route("/reminders")
def reminders_r():
    with reminders_lock:
        return jsonify([{"text":r["text"],"time":r["time"].strftime("%H:%M"),"id":r["id"]} for r in reminders])

@app.route("/screenshot/laptop")
def ss_laptop():
    try: return jsonify({"image":capture_laptop_screenshot()})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/screenshot/pc")
def ss_pc():
    try: return jsonify({"image":capture_pc_screenshot()})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/clipboard", methods=["GET", "POST"])
def clipboard_route():
    if request.method == "POST":
        data = request.json or {}
        text = data.get("text", "")
        success = set_clipboard_text(text)
        return jsonify({"success": success, "result": "Clipboard updated" if success else "Failed to update clipboard"})
    return jsonify({"text": get_clipboard_text()})

@app.route("/diagnostics")
def diagnostics_route():
    return jsonify(run_full_diagnostics())

@app.route("/network/devices")
def network_devices_route():
    return jsonify(get_network_devices())

@app.route("/study/flashcards", methods=["POST"])
def study_flashcards():
    data = request.json or {}
    topic = data.get("topic", "General Science")
    return jsonify({"topic": topic, "flashcards": generate_flashcards(topic)})

@app.route("/study/solve_screen", methods=["POST"])
def study_solve_screen():
    return jsonify({"result": solve_screen_problem()})

@app.route("/study/distraction_check", methods=["POST"])
def study_distraction_check():
    return jsonify({"result": check_focus_distractions()})

@app.route("/study/snap_windows", methods=["POST"])
def study_snap_windows():
    return jsonify({"result": snap_study_windows()})

@app.route("/study/session_log", methods=["POST"])
def study_session_log():
    data = request.json or {}
    hours = float(data.get("hours", 0.5))
    device = data.get("device", "laptop")
    subject = data.get("subject", "General Study")
    notes = data.get("notes", "")
    log_study_session(hours, device, subject, notes)
    return jsonify({"success": True, "message": "Study session logged successfully."})

@app.route("/study/solve_photo", methods=["POST"])
def study_solve_photo():
    d = request.json or {}
    img = d.get("image", "") or d.get("image_data", "")
    if not img:
        return jsonify({"error": "No image data provided."}), 400
    return jsonify({"result": solve_uploaded_image(img)})

@app.route("/youtube/play", methods=["POST"])
def youtube_play():
    d = request.json or {}
    q = d.get("query", "").strip()
    dev = d.get("device", "laptop")
    return jsonify(play_youtube(q, dev, return_dict=True))

@app.route("/files/search", methods=["GET", "POST"])
def files_search():
    if request.method == "POST":
        q = (request.json or {}).get("query", "") or (request.json or {}).get("q", "")
    else:
        q = request.args.get("query", "") or request.args.get("q", "")
    res = search_local_files(q)
    return jsonify({"query": q, "count": len(res), "files": res, "results": res})

@app.route("/files/open", methods=["POST"])
def files_open():
    d = request.json or {}
    fp = d.get("path", "")
    return jsonify({"result": open_local_file(fp)})

@app.route("/pc/terminal", methods=["POST"])
def pc_terminal_route():
    d = request.json or {}
    cmd = d.get("command", "").strip()
    if not cmd:
        return jsonify({"output": "", "error": "No command provided.", "code": 1, "ms": 0})
    return jsonify(execute_pc_terminal(cmd))

@app.route("/goals", methods=["GET", "POST"])
def goals_route():
    if request.method == "POST":
        d = request.json or {}
        text = d.get("text", "").strip()
        sub = d.get("subject", "General Study").strip()
        if not text:
            return jsonify({"error": "No text"}), 400
        return jsonify(add_goal(text, sub))
    return jsonify(load_goals())

@app.route("/goals/<int:gid>/toggle", methods=["POST"])
def goals_toggle(gid):
    return jsonify(toggle_goal(gid))

@app.route("/goals/<int:gid>", methods=["DELETE"])
def goals_del(gid):
    return jsonify(delete_goal(gid))

@app.route("/manifest.json")
def manifest():
    return jsonify({"name":"Jarvis","short_name":"Jarvis","start_url":"/","display":"standalone",
                    "background_color":"#0f172a","theme_color":"#0f172a",
                    "icons":[{"src":"https://cdn-icons-png.flaticon.com/512/888/888879.png","sizes":"512x512","type":"image/png"}]})

if __name__=="__main__":
    print("="*60)
    print("  JARVIS v3.0 — ALL FEATURES LOADED")
    print("  Open http://<laptop-ip>:5000")
    print("="*60)
    app.run(host="0.0.0.0",port=5000,debug=False,threaded=True)
