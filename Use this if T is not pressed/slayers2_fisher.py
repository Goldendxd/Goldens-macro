"""
Slayers 2 auto-fisher with GUI (Windows)
  python -m pip install mss opencv-python numpy requests rapidocr-onnxruntime
  python slayers2_fisher.py          (F6 start/stop, F7 set cast point, F8 quit)

Loop: cast -> minigame (auto-detect) -> collect -> recast
Collect: T prompt visible for ~1s -> hold T (re-press if it stays, release when it
vanishes, done when it stays gone) -> fish name read from the prompt -> webhook.
Rare items that go straight to the inventory (no T prompt) are caught by the item
popup box and sent to the webhook as "Auto-collected".
"""
import sys, os, re, json, time, ctypes, threading, queue, base64, difflib
from ctypes import wintypes
from datetime import datetime, timezone
try:
    import numpy as np, cv2, mss, requests
except Exception as _e:
    print(f"\nMISSING/BROKEN PACKAGE: {_e}\n\nRun this, then start again:\n"
          "  python -m pip install mss opencv-python numpy requests rapidocr-onnxruntime\n")
    input("Press Enter to close...")
    raise SystemExit
import tkinter as tk
from tkinter import scrolledtext

# ===================== SETTINGS =====================
LOST_RELEASE = 0.30   # release input briefly after a lost frame
LOST_TIMEOUT = 1.50   # only end the minigame after a sustained detector loss
CAST_WAIT   = 20.0    # s to wait for the minigame after a cast, else reset the rod
CAST_DELAY  = 0.6     # s after the cast click before looking for the minigame
PROMPT_WAIT = 5.0     # s after the minigame to wait for the T prompt / an item popup
COLLECT_MAX = 15.0    # s hard cap for one collect
MATCH       = 0.72    # template score that counts as "T prompt visible"
MATCH_KEEP  = 0.55    # while holding T the prompt only has to score this much (less jumpy)
REPRESS_CONFIRM = 0.3 # s the prompt must be back before T is pressed again
T_REPRESS_AFTER = 3.0 # re-press T if no claim popup has appeared
KEY_DOWN_RETRIES = 3  # repeated key-down packets reduce missed first presses
KEY_DOWN_GAP = 0.035
TOAST_STABLE_FRAMES = 2
TOAST_MIN_PX = 120    # bright text pixels that count as "item popup visible"
WATCH_FAIL_S, WATCH_DC_S = 60, 180
CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_TRACK = [1408, 308, 1472, 702]          # 1920x1080 defaults - drag the boxes if yours differ
DEFAULT_TBOX  = [880, 390, 1140, 480]           # where the T prompt shows up
DEFAULT_TOAST = [940, 560, 1240, 625]           # where "Fish x1" item popups show up
SHOT_AREA     = [480, 360, 1440, 780]           # highlight-message screenshot (prompt + popup), primary-monitor coords
THUMB_AREA    = [790, 310, 1210, 730]           # square crop shown beside the Fishing Log (character + prompt + popup)
FOOTER        = "Golden's Fishing Macro"

# Auto-detection colors (HSV, OpenCV scale). Tested on real Slayers 2 frames.
TARGET_LO, TARGET_HI = np.array([22, 100, 70], np.uint8), np.array([90, 255, 255], np.uint8)   # green/yellow box
SQUARE_LO, SQUARE_HI = np.array([0, 0, 150], np.uint8),  np.array([179, 90, 255], np.uint8)   # white/pale square

# The white "T" circle of the collect prompt (19x19 grayscale, cut from a real frame)
TPL_B64 = ("iVBORw0KGgoAAAANSUhEUgAAABMAAAATCAAAAABXO2kQAAAAnElEQVQYGWXBIU4DQRiG4fdLVU/AURDLEaorMJOxiKZJ3dyABFu56J5kkiY4JFwExH5M/l+x+zxaxH+2zJplNmTgeiM9vjHIwOs76WlmkIHPL75nDhMPE4MM/Pzq/sLlmd2eQSb0SiskmdArrZBkQq+0QpIJvdIKSSb0SiskmdArrZBkQq+0QpIJvdIKSSZ8nDkdSTIbMmvWIlaWP3luNfsKHfkrAAAAAElFTkSuQmCC")
TPL = cv2.imdecode(np.frombuffer(base64.b64decode(TPL_B64), np.uint8), cv2.IMREAD_GRAYSCALE)

# live-tunable from the GUI
TUNE  = dict(lead=0.07, damp=0.05, dead=4.0, confirm=1.0, miss=0.8, cycle=4.5, recast=0.8, invert=False)
STATS = dict(ok=0, auto=0, fail=0, streak=0, timeouts=0, t0=None)
CAUGHT = {}                                  # fish name -> count (the Fishing Log list)
LAST_CATCH = ""
LOGQ  = queue.Queue()

def log(m):
    m = time.strftime("%H:%M:%S  ") + m
    print(m); LOGQ.put(m)

ctypes.windll.user32.SetProcessDPIAware()
user32 = ctypes.windll.user32

# ===================== INPUT (SendInput) =====================
PUL = ctypes.POINTER(ctypes.c_ulong)
class KI(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", PUL)]
class MI(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", PUL)]
class _U(ctypes.Union):
    _fields_ = [("ki", KI), ("mi", MI)]
class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]
class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

def _send(i):
    sent = user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(i))
    if sent != 1:
        log(f"keyboard input was rejected by Windows (error {ctypes.get_last_error()})")
    return sent == 1
def mouse(flag): _send(INPUT(0, _U(mi=MI(0, 0, 0, flag, 0, None))))
def mdown(): mouse(0x0002)
def mup():   mouse(0x0004)
def key(scan, up=False):
    _send(INPUT(1, _U(ki=KI(0, scan, 0x0008 | (0x0002 if up else 0), 0, None))))
def key_vk(vk, up=False):
    """Send a virtual-key event for applications that do not accept scan codes."""
    _send(INPUT(1, _U(ki=KI(vk, 0, 0x0002 if up else 0, 0, None))))
def t_key(up=False):
    # Roblox can ignore scan-code T events on some keyboard layouts or clients.
    return key_vk(0x54, up)
def key_down_reliable(scan):
    """Send several key-down packets without an intervening key-up."""
    key(scan, True)
    for i in range(KEY_DOWN_RETRIES):
        key(scan)
        if i + 1 < KEY_DOWN_RETRIES:
            time.sleep(KEY_DOWN_GAP)
def click():
    mdown(); time.sleep(0.04); mup()
def tap_digit(k):
    k = str(k).strip()
    if len(k) != 1 or not k.isdigit(): return False
    scan = 0x0B if k == "0" else 0x01 + int(k)
    key(scan); time.sleep(0.08); key(scan, True); return True
def cursor_pos():
    p = POINT(); user32.GetCursorPos(ctypes.byref(p)); return [int(p.x), int(p.y)]

def vk_down(vk): return bool(user32.GetAsyncKeyState(vk) & 0x8000)
def roblox_focused():
    h = user32.GetForegroundWindow(); b = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(h, b, 256)
    return "roblox" in b.value.lower()
def focus_roblox():
    h = user32.FindWindowW(None, "Roblox")
    if h:
        user32.ShowWindow(h, 9); user32.SetForegroundWindow(h)
    return bool(h)

# ===================== CONFIG / CAPTURE =====================
def load_cfg():
    try: return json.load(open(CFG))
    except Exception: return {}
def save_cfg(c): json.dump(c, open(CFG, "w"), indent=2)
def update_cfg(**kw):
    c = load_cfg(); c.update(kw); save_cfg(c); return c

def grab(sct, box):
    """Absolute screen region [l,t,r,b] -> BGR numpy array. (mss instance must be made in the calling thread.)"""
    l, t, r, b = box
    img = np.asarray(sct.grab({"left": int(l), "top": int(t), "width": int(r - l), "height": int(b - t)}))
    return np.ascontiguousarray(img[:, :, :3])

# ===================== VISION (automatic) =====================
def blob(mask, minpx, gap, min_len, max_len):
    """Vertical center + size of the best run of rows with enough matching pixels."""
    prof = (mask > 0).sum(axis=1)
    idx = np.flatnonzero(prof >= minpx)
    if idx.size == 0: return None
    runs = np.split(idx, np.flatnonzero(np.diff(idx) > gap) + 1)
    runs = [r for r in runs if min_len <= (r[-1] - r[0] + 1) <= max_len]
    if not runs: return None
    r = max(runs, key=lambda r: r[-1] - r[0])
    return (r[0] + r[-1]) / 2.0, int(r[-1] - r[0] + 1)

def detect(sub):
    """Returns (target_box, white_square), each (center_y, size) or None."""
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    tm = cv2.inRange(hsv, TARGET_LO, TARGET_HI)
    sm = cv2.inRange(hsv, SQUARE_LO, SQUARE_HI)
    return blob(tm, 14, 45, 14, 240), blob(sm, 12, 3, 10, 60)

def prompt_score(img):
    """Best template match of the T circle in img -> (score, cx, cy) relative to img."""
    if img.shape[0] < TPL.shape[0] or img.shape[1] < TPL.shape[1]: return 0.0, 0, 0
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(g, TPL, cv2.TM_CCOEFF_NORMED))
    if not np.isfinite(mx): mx = 0.0
    return float(mx), loc[0] + TPL.shape[1] // 2, loc[1] + TPL.shape[0] // 2

def toast_visible(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    white = ((hsv[..., 1] < 60) & (hsv[..., 2] > 210)).astype(np.uint8)
    h, w = white.shape
    inner = white[int(h * 0.18):int(h * 0.88), int(w * 0.08):int(w * 0.92)]
    if int(inner.sum()) < TOAST_MIN_PX:
        return False
    n, _, stats, _ = cv2.connectedComponentsWithStats(inner, 8)
    parts = []
    for x, y, cw, ch, area in stats[1:]:
        if area >= 3 and 1 <= cw <= max(80, w // 2) and 2 <= ch <= max(24, h // 2):
            parts.append((x, y, cw, ch, area))
    if len(parts) < 2:
        return False
    text_rows = [p for p in parts if 0.20 * h <= p[1] <= 0.75 * h]
    return len(text_rows) >= 2

OCR_SKIP = {"collect", "t", "e", "fish", "item", "unknown", "catch"}

def clean_name(text):
    text = re.sub(r"[^A-Za-z0-9 '\-.]", "", str(text or ""))
    text = " ".join(text.split()).strip(" .-'")
    text = re.sub(r"\s*[xX×]\s*\d+\s*$", "", text).strip()
    if text.lower().startswith("collect"):
        text = text[7:].strip(" :-")
    if len(text) > 40:
        text = text[:40].rsplit(" ", 1)[0].strip()
    letters = sum(ch.isalpha() for ch in text)
    if letters < 3 or text.lower() in OCR_SKIP:
        return ""
    return text

def name_variants(img):
    """Create OCR-friendly views of a small white-on-dark name popup."""
    if img is None or img.size == 0:
        return []
    up = cv2.resize(img, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if cv2.countNonZero(binary) < binary.size // 2:
        binary = cv2.bitwise_not(binary)
    bordered = cv2.copyMakeBorder(binary, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=0)
    return [up, cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR),
            cv2.cvtColor(bordered, cv2.COLOR_GRAY2BGR)]

def choose_name(candidates):
    names = [clean_name(x) for x in candidates]
    names = [x for x in names if x]
    if not names:
        return ""
    groups = []
    for name in names:
        for group in groups:
            if difflib.SequenceMatcher(None, name.lower(), group[0].lower()).ratio() >= 0.72:
                group.append(name)
                break
        else:
            groups.append([name])
    best = max(groups, key=lambda g: (len(g), max(map(len, g))))
    return max(best, key=len)

def is_highlight(name, words):
    requested = {x.strip().casefold() for x in (words or "").split(",") if x.strip()}
    return bool(requested and (name or "").strip().casefold() in requested)

# ===================== WEBHOOK =====================
def make_embed(title, color, fields=(), desc=None):
    e = {"title": title, "color": color, "footer": {"text": FOOTER},
         "timestamp": datetime.now(timezone.utc).isoformat()}
    if fields: e["fields"] = [{"name": n, "value": str(v), "inline": True} for n, v in fields]
    if desc: e["description"] = desc[:4000]
    return e

def post_webhook(url, embed, content=None, img=None, user_id=None):
    """Send an embed (optionally with a screenshot and a user ping). Runs in a background thread."""
    if not url: return
    payload = {"embeds": [embed]}
    if content: payload["content"] = content
    if user_id and user_id in (content or ""): payload["allowed_mentions"] = {"users": [user_id]}
    def go():
        try:
            if img:
                embed["image"] = {"url": "attachment://shot.jpg"}
                r = requests.post(url, data={"payload_json": json.dumps(payload)},
                                  files={"file": ("shot.jpg", img, "image/jpeg")}, timeout=15)
            else:
                r = requests.post(url, json=payload, timeout=15)
            if r.status_code >= 300: log(f"webhook HTTP {r.status_code}")
        except Exception as e: log(f"webhook error: {e}")
    threading.Thread(target=go, daemon=True).start()

def run_time():
    el = int(time.time() - (STATS["t0"] or time.time()))
    return f"{el // 3600}:{el % 3600 // 60:02d}:{el % 60:02d}"

LOGSTATE = dict(url=None, msg_id=None)       # the one Discord message we keep editing
LOGJOBS = queue.Queue()

def build_log_embed(words):
    """'Fishing Log' embed: totals line + one 'name xN' row per fish, most caught first."""
    got = STATS["ok"] + STATS["auto"]
    head = f"Caught {got} • Escaped {STATS['fail']} • Running for {run_time()}"
    rows = sorted(CAUGHT.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    lines = []
    for name, n in rows[:40]:
        star = is_highlight(name, words)
        lines.append(f"{'⭐ ' if star else '🐟 '}{f'**{name}**' if star else name} x{n}")
    if len(rows) > 40: lines.append(f"...and {len(rows) - 40} more")
    return {
        "title": "Fishing Log",
        "color": 0x57F287,
        "footer": {"text": FOOTER},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [
            {"name": "Last catch", "value": LAST_CATCH or "Waiting for a catch", "inline": True},
        ],
        "description": ("\n".join(lines) if lines else "No catches recorded yet.")[:4096],
    }

def push_log_message(url, embed, jpg):
    """Edit the existing Fishing Log message (new list + new side picture); create it if needed."""
    if LOGSTATE["url"] != url: LOGSTATE.update(url=url, msg_id=None)
    base = url.split("?")[0].rstrip("/")
    if jpg: embed["image"] = {"url": "attachment://shot.jpg"}
    def send(method, target, edit):
        payload = {"embeds": [embed]}
        if not jpg: return requests.request(method, target, json=payload, timeout=15)
        if edit: payload["attachments"] = [{"id": 0, "filename": "shot.jpg"}]   # swap the old picture for the new one
        return requests.request(method, target, data={"payload_json": json.dumps(payload)},
                                files={"files[0]": ("shot.jpg", jpg, "image/jpeg")}, timeout=15)
    if LOGSTATE["msg_id"]:
        r = send("PATCH", f"{base}/messages/{LOGSTATE['msg_id']}", True)
        if r.status_code < 300: return
        LOGSTATE["msg_id"] = None                                  # deleted or broken: start a fresh message
        if r.status_code != 404: log(f"webhook edit HTTP {r.status_code}, sending a new log message")
    r = send("POST", base + "?wait=true", False)
    if r.status_code < 300:
        try: LOGSTATE["msg_id"] = r.json().get("id")
        except Exception: pass
    else: log(f"webhook HTTP {r.status_code}")

def _log_worker():
    while True:
        job = LOGJOBS.get()
        try:
            while True: job = LOGJOBS.get_nowait()                 # only the newest update matters
        except queue.Empty: pass
        try: push_log_message(*job)
        except Exception as e: log(f"webhook error: {e}")
threading.Thread(target=_log_worker, daemon=True).start()

# ===================== BOT =====================
class Bot:
    def __init__(s, cfg):
        s.c = cfg; s.holding = False; s.running = True; s.ocr = None; s.sct = None
        s.toast_at_start = False; s.ocr_lock = threading.Lock()
        s.last_catch = time.time(); s.dc_sent = 0; s.fail_sent = False
        s.popup_claimed = False
        s.popup_name_job = None

    # ---- basics
    def grab(s, box): return grab(s.sct, box)
    def stop(s): return (not s.running) or (not roblox_focused())
    def sleep(s, sec):
        end = time.time() + sec
        while time.time() < end and not s.stop(): time.sleep(0.01)
    def hold(s, on):
        if on and not s.holding: mdown(); s.holding = True
        elif not on and s.holding: mup(); s.holding = False
    def mon_box(s, box):
        m = s.sct.monitors[1]; l, t, r, b = box
        return [m["left"] + max(0, l), m["top"] + max(0, t),
                m["left"] + min(m["width"], r), m["top"] + min(m["height"], b)]
    def clamp_abs(s, box):
        m = s.sct.monitors[1]; l, t, r, b = box
        return [max(m["left"], l), max(m["top"], t),
                min(m["left"] + m["width"], r), min(m["top"] + m["height"], b)]
    def screenshot(s):
        try:
            img = s.grab(s.mon_box(SHOT_AREA))
            return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
        except Exception: return None

    # ---- cast / rod
    def cast(s):
        pt = s.c.get("cast_pt")
        if pt: user32.SetCursorPos(int(pt[0]), int(pt[1])); time.sleep(0.05)
        click(); s.sleep(CAST_DELAY)
    def reset_rod(s):
        k = s.c.get("rod_key", "")
        if not k: return
        log(f"rod reset (key {k} x2)")
        for _ in range(2):
            tap_digit(k); s.sleep(0.7)

    # ---- minigame
    def wait_minigame(s):
        hits = 0; t0 = time.time()
        while time.time() - t0 < CAST_WAIT:
            if s.stop(): return False
            tg, sq = detect(s.grab(s.c["track"]))
            hits = hits + 1 if (tg and sq) else 0
            if hits >= 3: return True
            time.sleep(0.01)
        return False

    def play(s):
        c = s.c
        tp = sp = lt = ls = None; tv = sv = 0.0       # target pos/vel, square pos/vel
        last = time.perf_counter()
        lost_since = None
        last_action = last
        latency = 0.10
        min_action_gap = 0.035
        dead = max(1.0, TUNE["dead"])
        while not s.stop():
            sub = s.grab(c["track"])
            now = time.perf_counter(); dt = min(max(now - last, 1e-3), 0.1); last = now
            tg, sq = detect(sub)
            if tg is None and sq is None:
                if lost_since is None: lost_since = now
                if now - lost_since >= LOST_TIMEOUT: break
                if now - lost_since >= LOST_RELEASE:
                    s.hold(False)
                continue
            lost_since = None
            if tg:
                tp = tg[0]
                if lt is not None: tv = 0.5 * tv + 0.5 * (tp - lt) / dt
                lt = tp
            if sq:
                sp = sq[0]
                if ls is not None: sv = 0.5 * sv + 0.5 * (sp - ls) / dt
                ls = sp
            if tp is None or sp is None: continue
            # Predict both moving edges. The wider switch band prevents input
            # chatter while the detector is quantized or briefly noisy.
            predicted_square = sp + sv * latency
            predicted_target = tp + tv * latency
            u = predicted_square - predicted_target
            u += TUNE["damp"] * (sv - tv)
            if TUNE["invert"]: u = -u
            holding = s.holding
            should_hold = u > (dead * 0.8 if holding else dead)
            if holding:
                should_hold = u > -dead * 1.25
            if should_hold != holding and now - last_action >= min_action_gap:
                s.hold(should_hold)
                last_action = now
        s.hold(False)

    # ---- reading
    def find_prompt(s):
        """(score, cx, cy) of the T circle. Looks in the T box plus a margin so a swinging prompt is still found."""
        l, t, r, b = s.c["tbox"]
        padx, pady = max(140, r - l), max(70, (b - t) // 2)
        box = s.clamp_abs([l - padx, t - pady, r + padx, b + pady])
        sc, x, y = prompt_score(s.grab(box))
        return sc, box[0] + x, box[1] + y
    def toast_present(s): return toast_visible(s.grab(s.c["toast"]))

    def ocr_lines(s, img):
        try:
            with s.ocr_lock:
                if s.ocr is None:
                    from rapidocr_onnxruntime import RapidOCR; s.ocr = RapidOCR()
                res, _ = s.ocr(cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC))
            if not res: return []
            return [t.strip() for b, t, c in sorted(res, key=lambda x: x[0][0][1]) if t.strip()]
        except Exception as e:
            log(f"OCR error: {e}"); return []
    def parse_label(s, lines):
        return choose_name(lines)
    def read_name_region(s, box):
        candidates = []
        crop = s.grab(s.clamp_abs(box))
        return s.read_name_region_from_image(crop)
    def read_name_region_from_image(s, crop):
        candidates = []
        for variant in name_variants(crop):
            candidates.extend(s.ocr_lines(variant))
        return choose_name(candidates)
    def thumb(s):
        """Small square picture of what the bot is doing right now (shown beside the Fishing Log)."""
        try:
            img = cv2.resize(s.grab(s.mon_box(THUMB_AREA)), (256, 256), interpolation=cv2.INTER_AREA)
            return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
        except Exception: return None
    def read_toast(s):
        l, t, r, b = s.c["toast"]
        return s.read_name_region([l - 20, t - 18, r + 20, b + 18])
    def read_toast_from_screenshot(s, jpg):
        if not jpg:
            return ""
        try:
            frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                return ""
            sl, st, _, _ = SHOT_AREA
            l, t, r, b = s.c["toast"]
            x0, y0 = max(0, l - sl), max(0, t - st)
            x1, y1 = min(frame.shape[1], r - sl), min(frame.shape[0], b - st)
            if x1 <= x0 or y1 <= y0:
                return ""
            return s.read_name_region_from_image(frame[y0:y1, x0:x1])
        except Exception as e:
            log(f"Screenshot OCR error: {e}")
            return ""
    # ---- collect (T prompt or auto item)
    def collect(s):
        """Returns ('claimed'|'auto', name, jpg) or None (fish lost)."""
        deadline = time.time() + PROMPT_WAIT
        seen_since = None; toast_ok = not s.toast_at_start; next_toast = 0.0
        while True:
            if s.stop(): return None
            now = time.time()
            sc, cx, cy = s.find_prompt()
            if sc >= MATCH:
                if seen_since is None: seen_since = now
                if now - seen_since >= TUNE["confirm"]:
                    return s.claim(cx, cy)
            else:
                seen_since = None
                if now > deadline: return None
                if now >= next_toast:                         # lost item: straight to inventory, no T
                    next_toast = now + 0.25
                    if not s.toast_present(): toast_ok = True
                    elif toast_ok:
                        s.sleep(0.3)
                        name = s.read_toast() or "Unknown item"
                        log(f"item went straight to inventory: {name}")
                        return "auto", name, s.screenshot()
            time.sleep(0.03)

    def claim(s, cx, cy):
        """Hold T while the prompt is there. Re-press if it stays, release when it vanishes,
        done once it has stayed gone. The name is read (in the background) when the first press starts."""
        s.popup_name_job = None
        t_start = time.time(); held = False; presses = 0; last_sc = 0.0
        held_at = hold_from = 0.0; present_since = None
        names = []; threads = []
        toast_clear = not s.toast_at_start
        toast_hits = 0
        toast_seen = False

        def read_async():                                         # scan the T area for the fish name
            crop = s.grab(s.clamp_abs([cx + 24, cy - 22, cx + 300, cy + 24]))
            def work():
                n = s.parse_label(s.ocr_lines(crop))
                if n: names.append(n)
            th = threading.Thread(target=work, daemon=True); th.start(); threads.append(th)

        try:
            while time.time() - t_start < COLLECT_MAX and not s.stop():
                now = time.time()
                sc, px, py = s.find_prompt(); last_sc = sc
                present = sc >= (MATCH_KEEP if held else MATCH)
                toast_now = s.toast_present()
                if not toast_now:
                    toast_clear = True
                    toast_hits = 0
                elif toast_clear:
                    toast_hits += 1
                    if toast_hits >= TOAST_STABLE_FRAMES:
                        toast_seen = True
                        s.popup_claimed = True
                        if held:
                            t_key(True)
                            held = False
                        log("claim popup detected in orange box")
                        name_result = []
                        popup_frames = [s.grab(s.clamp_abs(s.c["toast"]))]
                        popup_frames.append(s.grab(s.clamp_abs(s.c["toast"])))
                        def read_popup_name():
                            candidates = [
                                s.read_name_region_from_image(frame)
                                for frame in popup_frames
                            ]
                            name_result.append(choose_name(candidates))
                        s.popup_name_job = (threading.Thread(target=read_popup_name, daemon=True), name_result)
                        s.popup_name_job[0].start()
                        break
                if present: cx, cy = px, py
                if not held:
                    if present:
                        if present_since is None: present_since = now
                        if presses == 0 or now - present_since >= REPRESS_CONFIRM:   # re-press needs a steady look
                            t_key(); held = True; presses += 1; held_at = hold_from = time.time()
                            present_since = None
                            log(f"holding T (press {presses})")
                            if presses == 1 or not names: read_async()
                    else:
                        present_since = None
                        if presses and not toast_seen and now - held_at >= T_REPRESS_AFTER:
                            t_key(); held = True; held_at = time.time()
                            log(f"re-pressing T after {T_REPRESS_AFTER:.0f}s without claim popup")
                else:
                    if not toast_seen and now - held_at >= T_REPRESS_AFTER:
                        t_key(True); time.sleep(0.1); t_key()
                        held_at = time.time()
                        log(f"re-pressing T after {T_REPRESS_AFTER:.0f}s without claim popup (score {last_sc:.2f})")
                time.sleep(0.03)
        finally:
            if held: t_key(True)
        if s.stop() or presses == 0: return None
        for th in threads:
            th.join(timeout=0.0)
        log(f"collected after {presses} press(es)")
        if not s.popup_claimed:
            log("no orange claim popup; treating catch as lost")
            return None
        return "claimed", "", s.screenshot()

    # ---- alerts (no fish for a while)
    def alert(s, title, desc):
        try:
            with mss.mss() as sct:
                m = sct.monitors[1]
                full = grab(sct, [m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]])
            full = cv2.resize(full, (960, int(full.shape[0] * 960 / full.shape[1])))
            img = cv2.imencode(".jpg", full, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
        except Exception: img = None
        uid = s.c.get("user_id", "")
        post_webhook(s.c.get("webhook"), make_embed(title, 0xED4245, [("Catches", STATS["ok"] + STATS["auto"]),
                     ("Running for", run_time())], desc), f"<@{uid}>" if uid else None, img, uid)
    def watch(s):
        while s.running:
            time.sleep(1.0)
            if not s.c.get("webhook"): continue
            idle = time.time() - s.last_catch
            if s.c.get("w_dc") and idle >= (s.dc_sent + 1) * WATCH_DC_S:
                s.alert("Possibly disconnected OR griefed", f"No fish caught in the last {int(idle) // 60} minutes.")
                s.dc_sent = int(idle // WATCH_DC_S); continue
            if s.c.get("w_fail") and not s.fail_sent and idle >= WATCH_FAIL_S:
                s.alert("Something failed :(", f"No fish caught in the last {WATCH_FAIL_S} seconds.")
                s.fail_sent = True

    # ---- catch webhook
    def push_log(s):
        url = s.c.get("webhook")
        if url: LOGJOBS.put((url, build_log_embed(s.c.get("highlight")), s.thumb()))
    def report(s, kind, name, img):
        global LAST_CATCH
        hl = is_highlight(name, s.c.get("highlight")); auto = kind == "auto"
        LAST_CATCH = name
        STATS["auto" if auto else "ok"] += 1; STATS["streak"] += 1
        CAUGHT[name] = CAUGHT.get(name, 0) + 1
        s.last_catch = time.time(); s.dc_sent = 0; s.fail_sent = False
        log(f"{'AUTO' if auto else 'CAUGHT'}{' ⭐' if hl else ''}: {name}")
        s.push_log()                                              # edits the Fishing Log message
    def report_async(s, kind, img):
        job = s.popup_name_job
        def finish():
            name = ""
            if job:
                thread, result = job
                thread.join(timeout=3.0)
                name = choose_name(result)
            screenshot_name = s.read_toast_from_screenshot(img)
            name = choose_name([name, screenshot_name])
            s.report(kind, name or "Unknown catch", img)
        threading.Thread(target=finish, daemon=True).start()

    # ---- main loop
    def run(s):
        s.sct = mss.mss()
        if STATS["t0"] is None: STATS["t0"] = time.time()
        threading.Thread(target=s.watch, daemon=True).start()
        if s.ocr is None: s.ocr_lines(np.zeros((20, 60, 3), np.uint8))   # warm up OCR so the first read is fast
        log("Bot started")
        try:
            warn = 0.0
            while s.running:
                if not roblox_focused():
                    if time.time() - warn > 5:
                        log("Waiting: Roblox is not the active window - click the game"); warn = time.time()
                    time.sleep(0.1); continue
                s.cast()
                if not s.wait_minigame():
                    if not s.stop():
                        STATS["timeouts"] += 1; log("No minigame - recasting"); s.reset_rod()
                    continue
                s.toast_at_start = s.toast_present()
                log("Minigame started")
                s.play()
                if s.stop(): continue
                res = s.collect()
                if res:
                    if s.popup_claimed and not res[1]:
                        s.report_async(res[0], res[2])
                    else:
                        s.report(*res)
                elif not s.stop():
                    STATS["fail"] += 1; STATS["streak"] = 0; log("Fish lost / nothing collected"); s.push_log()
                s.sleep(0.05 if s.popup_claimed else TUNE["recast"])
                s.popup_claimed = False
        except Exception as e:
            log(f"BOT ERROR: {e}")
        finally:
            s.running = False; s.hold(False)
            try: t_key(True)
            except Exception: pass
            try: s.sct.close()
            except Exception: pass
            log("Bot stopped")

# ===================== ON-SCREEN BOXES =====================
KEY, M = "#010101", 12      # transparent color, margin (frame is drawn OUTSIDE the captured area)
class BoxOverlay:
    """Always-on-top frame. Inside is click-through and never captured.
    Drag the border to move, drag the corner square to resize. Position is saved automatically."""
    def __init__(s, root, box, color, on_change):
        s.color, s.on_change, s.mode = color, on_change, None
        w = s.w = tk.Toplevel(root)
        w.overrideredirect(True); w.attributes("-topmost", True)
        w.attributes("-transparentcolor", KEY); w.configure(bg=KEY)
        x1, y1, x2, y2 = box
        w.geometry(f"{x2 - x1 + 2 * M}x{y2 - y1 + 2 * M}+{x1 - M}+{y1 - M}")
        s.cv = tk.Canvas(w, bg=KEY, highlightthickness=0); s.cv.pack(fill="both", expand=True)
        s.cv.bind("<Configure>", lambda e: s.draw())
        s.cv.bind("<ButtonPress-1>", s.press); s.cv.bind("<B1-Motion>", s.drag)
        s.cv.bind("<ButtonRelease-1>", s.release)
    def draw(s):
        s.cv.delete("all"); W, H = s.w.winfo_width(), s.w.winfo_height()
        s.cv.create_rectangle(2, 2, W - 2, H - 2, outline=s.color, width=4)
        s.cv.create_rectangle(W - 14, H - 14, W - 1, H - 1, fill=s.color, outline="")
    def press(s, e):
        W, H = s.w.winfo_width(), s.w.winfo_height()
        s.mx, s.my = e.x_root, e.y_root
        s.mode = "resize" if (e.x > W - 20 and e.y > H - 20) else "move"
        s.ox, s.oy, s.ow, s.oh = s.w.winfo_x(), s.w.winfo_y(), W, H
    def drag(s, e):
        dx, dy = e.x_root - s.mx, e.y_root - s.my
        if s.mode == "move": s.w.geometry(f"+{s.ox + dx}+{s.oy + dy}")
        elif s.mode == "resize": s.w.geometry(f"{max(2 * M + 24, s.ow + dx)}x{max(2 * M + 24, s.oh + dy)}")
    def release(s, e):
        if s.mode: s.on_change(s.box())
        s.mode = None
    def box(s):
        s.w.update_idletasks()
        x, y = s.w.winfo_x(), s.w.winfo_y()
        return [x + M, y + M, x + s.w.winfo_width() - M, y + s.w.winfo_height() - M]
    def show(s, on):
        if on: s.w.deiconify(); s.w.attributes("-topmost", True); s.w.lift()
        else: s.w.withdraw()

# ===================== GUI =====================
BG, PANEL, CARD, FG, MUTED, ACC, RED = (
    "#10141c", "#18202c", "#202b3a", "#edf3fb", "#9aa9bd", "#54e38e", "#ff7180"
)

class App:
    def __init__(s):
        s.cfg = load_cfg(); TUNE.update(s.cfg.get("tune5", {}))
        s.bot = s.thread = None; s.cast_pt = s.cfg.get("cast_pt")
        s.prev = {0x75: False, 0x76: False, 0x77: False}
        r = s.root = tk.Tk(); r.title("Slayers 2 Fisher"); r.geometry("1180x820"); r.minsize(1000, 700); r.configure(bg=BG)
        r.protocol("WM_DELETE_WINDOW", s.close)
        s.psct = mss.mss(); s.pimg = None            # preview capture (main thread)

        left = tk.Frame(r, bg=BG, width=320); left.pack(side="left", fill="y", padx=(14, 8), pady=12)
        mid = tk.Frame(r, bg=BG); mid.pack(side="left", fill="both", expand=True, padx=8, pady=12)
        right = tk.Frame(r, bg=BG, width=190); right.pack(side="right", fill="y", padx=(8, 14), pady=12)

        # ---- left: controls
        tk.Label(left, text="SLAYERS 2", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(left, text="Fishing macro", bg=BG, fg=FG, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        s.status = tk.Label(left, text="Idle", bg=BG, fg=MUTED, font=("Segoe UI", 10)); s.status.pack(anchor="w", pady=(2, 8))
        s.btn = s.button(left, "▶  Start fishing  (F6)", s.toggle, ACC, "#08110d"); s.btn.pack(fill="x", pady=(0, 12), ipady=3)

        s.track_ov = BoxOverlay(r, s.cfg.get("track") or DEFAULT_TRACK, "#ff2bd6", lambda b: update_cfg(track=b))
        s.tbox_ov = BoxOverlay(r, s.cfg.get("tbox") or DEFAULT_TBOX, "#22d3ee", lambda b: update_cfg(tbox=b))
        s.toast_ov = BoxOverlay(r, s.cfg.get("toast") or DEFAULT_TOAST, "#fb923c", lambda b: update_cfg(toast=b))
        s.section(left, "SCREEN AREAS")
        s.show_track = s.check(left, "Pink  ·  fishing minigame", True, lambda: s.track_ov.show(s.show_track.get()))
        s.show_t = s.check(left, "Cyan  ·  T prompt", True, lambda: s.tbox_ov.show(s.show_t.get()))
        s.show_toast = s.check(left, "Orange  ·  claimed item popup", True, lambda: s.toast_ov.show(s.show_toast.get()))
        s.top = s.check(left, "Keep window on top", False, lambda: r.attributes("-topmost", s.top.get()))
        s.prev_on = s.check(left, "Show live detection preview", True, lambda: None)

        cp = tk.Frame(left, bg=BG); cp.pack(fill="x", padx=10, pady=(6, 2))
        s.button(cp, "Set cast point (F7)", s.set_cast).pack(side="left")
        s.cast_lbl = tk.Label(cp, text="", bg=BG, fg=FG, font=("Consolas", 9)); s.cast_lbl.pack(side="left", padx=8)
        s.show_cast()

        s.section(left, "NOTIFICATIONS")
        s.web = s.entry(left, "Webhook URL", s.cfg.get("webhook", ""), show="•")
        s.uid = s.entry(left, "User ID (optional)", s.cfg.get("user_id", ""))
        s.hl = s.entry(left, "Highlight words", s.cfg.get("highlight", "Ore"))
        s.rod = s.entry(left, "Rod hotbar key", s.cfg.get("rod_key", "5"))
        s.w_fail = s.check(left, "Alert after 60s with no fish", s.cfg.get("w_fail", False), lambda: None)
        s.w_dc = s.check(left, 'Ping every 180s ("possibly disconnected")', s.cfg.get("w_dc", True), lambda: None)
        b2 = tk.Frame(left, bg=BG); b2.pack(fill="x", padx=10, pady=4)
        s.button(b2, "Save", s.save_web).pack(side="left", expand=True, fill="x", padx=(0, 2))
        s.button(b2, "Test webhook", s.test_web).pack(side="left", expand=True, fill="x", padx=(2, 2))
        s.button(b2, "Reset log", s.reset_log).pack(side="left", expand=True, fill="x", padx=(2, 0))

        # ---- mid: tuning, stats, log
        tk.Label(mid, text="CONTROL CENTER", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10)
        tk.Label(mid, text="Detection tuning", bg=BG, fg=FG, font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=10, pady=(0, 6))
        s.slider(mid, "Prediction lead", "lead", 0, 0.25, 0.005)
        s.slider(mid, "Motion braking", "damp", 0, 0.2, 0.005)
        s.slider(mid, "Deadband (px)", "dead", 0, 12, 0.5)
        s.slider(mid, "Prompt confirm (s)", "confirm", 0, 3, 0.1)
        s.slider(mid, "Detector smoothing (s)", "miss", 0.2, 2.5, 0.1)
        s.slider(mid, "Recast delay (s)", "recast", 0, 4, 0.1)
        s.inv = s.check(mid, "Reverse minigame direction", TUNE["invert"], lambda: TUNE.update(invert=s.inv.get()))
        s.stats = tk.Label(mid, text="", bg=PANEL, fg=FG, justify="left", anchor="w", font=("Consolas", 10), padx=8, pady=6)
        s.stats.pack(fill="x", padx=10, pady=8)
        s.logbox = scrolledtext.ScrolledText(mid, height=12, bg=PANEL, fg=FG, relief="flat", font=("Consolas", 9), state="disabled")
        s.logbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # ---- right: preview
        tk.Label(right, text="LIVE VIEW", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(pady=(0, 2))
        tk.Label(right, text="Detection", bg=BG, fg=FG, font=("Segoe UI", 15, "bold")).pack()
        s.det = tk.Label(right, text="", bg=BG, fg=FG, font=("Consolas", 10)); s.det.pack()
        s.pv = tk.Canvas(right, width=150, height=740, bg=PANEL, highlightthickness=0); s.pv.pack(pady=6)

        log("Drag the pink box over the minigame track, the cyan box over where the T prompt shows, "
            "the orange box over the item popup. Press F6 to start.")
        s.tick()

    # ---- widgets
    def button(s, p, text, cmd, bg=PANEL, fg=FG):
        return tk.Button(p, text=text, command=cmd, bg=bg, fg=fg, relief="flat", activebackground="#2a2f3c",
                         activeforeground=FG, font=("Segoe UI", 10, "bold"), pady=5, cursor="hand2")
    def section(s, p, text):
        tk.Label(p, text=text, bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 2))
    def check(s, p, text, val, cmd):
        v = tk.BooleanVar(value=val)
        tk.Checkbutton(p, text=text, variable=v, bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
                       activeforeground=FG, anchor="w", command=cmd).pack(fill="x", padx=10)
        return v
    def entry(s, p, label, val, show=None):
        f = tk.Frame(p, bg=BG); f.pack(fill="x", padx=10, pady=2)
        tk.Label(f, text=label, width=18, anchor="w", bg=BG, fg=MUTED).pack(side="left")
        e = tk.Entry(f, bg=CARD, fg=FG, insertbackground=FG, relief="flat", show=show,
                     highlightthickness=1, highlightbackground="#2d3b4e", highlightcolor=ACC)
        e.insert(0, val); e.pack(side="left", fill="x", expand=True, ipady=3)
        return e
    def slider(s, p, label, k, lo, hi, res):
        f = tk.Frame(p, bg=BG); f.pack(fill="x", padx=10)
        tk.Label(f, text=label, width=18, anchor="w", bg=BG, fg=FG).pack(side="left")
        sc = tk.Scale(f, from_=lo, to=hi, resolution=res, orient="horizontal", bg=BG, fg=FG, troughcolor=PANEL,
                      highlightthickness=0, command=lambda v, k=k: TUNE.__setitem__(k, float(v)))
        sc.set(TUNE[k]); sc.pack(side="left", fill="x", expand=True)

    # ---- actions
    def running(s): return bool(s.thread and s.thread.is_alive() and s.bot and s.bot.running)
    def show_cast(s): s.cast_lbl.config(text=f"{s.cast_pt[0]}, {s.cast_pt[1]}" if s.cast_pt else "not set (casts where the mouse is)")
    def set_cast(s):
        s.cast_pt = cursor_pos(); s.show_cast(); update_cfg(cast_pt=s.cast_pt); log(f"Cast point set: {s.cast_pt}")

    def save_all(s):
        return update_cfg(webhook=s.web.get().strip(), user_id="".join(c for c in s.uid.get() if c.isdigit()),
                          highlight=s.hl.get().strip(), rod_key=s.rod.get().strip(),
                          w_fail=bool(s.w_fail.get()), w_dc=bool(s.w_dc.get()), tune5=dict(TUNE),
                          track=s.track_ov.box(), tbox=s.tbox_ov.box(), toast=s.toast_ov.box(), cast_pt=s.cast_pt)
    def save_web(s): s.save_all(); log("Saved")
    def test_web(s):
        c = s.save_all()
        if not c.get("webhook"): log("Paste a webhook URL first"); return
        uid = c.get("user_id", "")
        post_webhook(c["webhook"], make_embed("Webhook works :)", 0x5865F2, desc=f"Test from {FOOTER}."),
                     f"<@{uid}>" if uid else None, None, uid)
        log("Test webhook sent")

    def reset_log(s):
        CAUGHT.clear(); STATS.update(ok=0, auto=0, fail=0, streak=0, timeouts=0, t0=time.time() if s.running() else None)
        LOGSTATE["msg_id"] = None; log("Log reset - the next catch starts a new Fishing Log message")

    def toggle(s):
        if s.running():
            s.bot.running = False; s.save_all(); return
        if s.thread and s.thread.is_alive(): return                 # still shutting down
        cfg = s.save_all()
        s.bot = Bot(dict(cfg)); s.thread = threading.Thread(target=s.bot.run, daemon=True); s.thread.start()
        if not roblox_focused():
            if not focus_roblox(): log("Couldn't find a window titled 'Roblox' - click the game manually")

    def close(s):
        if s.bot: s.bot.running = False
        try: s.save_all()
        except Exception: pass
        time.sleep(0.2); s.root.destroy(); os._exit(0)

    def preview(s):
        if not s.prev_on.get() or not s.show_track.get(): return
        try:
            sub = grab(s.psct, s.track_ov.box())
            tg, sq = detect(sub); view = sub.copy(); W = view.shape[1]
            if tg: cv2.rectangle(view, (0, int(tg[0] - tg[1] / 2)), (W - 1, int(tg[0] + tg[1] / 2)), (0, 255, 0), 1)
            if sq: cv2.rectangle(view, (3, int(sq[0] - sq[1] / 2)), (W - 4, int(sq[0] + sq[1] / 2)), (0, 0, 255), 1)
            k = max(1, min(140 // max(W, 1), 730 // max(view.shape[0], 1), 4))
            s.pimg = tk.PhotoImage(data=base64.b64encode(cv2.imencode(".png", view)[1].tobytes())).zoom(k)
            s.pv.delete("all"); s.pv.create_image(75, 4, anchor="n", image=s.pimg)
            ts, _, _ = prompt_score(grab(s.psct, s.tbox_ov.box()))
            s.det.config(text=f"T:{'OK' if tg else '--'} S:{'OK' if sq else '--'} P:{'OK' if ts >= MATCH else '--'}",
                         fg=ACC if (tg and sq) else FG)
        except Exception:
            pass

    def tick(s):
        while True:
            try: m = LOGQ.get_nowait()
            except queue.Empty: break
            s.logbox.config(state="normal"); s.logbox.insert("end", m + "\n"); s.logbox.see("end")
            s.logbox.config(state="disabled")
        for vk in (0x75, 0x76, 0x77):
            d = vk_down(vk)
            if d and not s.prev[vk]:
                if vk == 0x77: s.close()
                elif vk == 0x76: s.set_cast()
                else: s.toggle()
            s.prev[vk] = d
        alive = bool(s.thread and s.thread.is_alive()); foc = roblox_focused()
        s.btn.config(text="Stop (F6)" if s.running() else "Start (F6)", bg=RED if s.running() else ACC)
        txt = ("RUNNING" if foc else "RUNNING - click the Roblox window") if s.running() \
              else "Stopping..." if alive else "Idle"
        s.status.config(text=txt, fg=(ACC if foc else "#facc15") if s.running() else FG)
        got = STATS["ok"] + STATS["auto"]; tot = got + STATS["fail"]
        s.stats.config(text=f"Catches {got} (auto {STATS['auto']})   Fails {STATS['fail']}   Success {100 * got // max(tot, 1)}%\n"
                            f"Streak {STATS['streak']}   No-minigame {STATS['timeouts']}   Session {run_time() if STATS['t0'] else '0:00:00'}")
        s.preview()
        s.root.after(100, s.tick)

if __name__ == "__main__":
    try:
        App().root.mainloop()
    except SystemExit:
        raise
    except BaseException:
        import traceback; traceback.print_exc()
        input("\nCRASHED - send me the error above. Press Enter to close...")
