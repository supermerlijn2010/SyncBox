# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SyncBox - keep folders (or whole drives) the same on all your computers.

All computers sync through one server:
  * your Raspberry Pi running syncbox_web.py (http://raspberrypi.local), or
  * any shared folder all computers can reach (USB stick, NAS, Google Drive).

  * Install: on the SyncBox website click "Download SyncBox for Windows" and
    run it. Or double-click "Install SyncBox.bat" next to this file.
  * Pick folders, your whole user folder, or a whole drive in the window, or
    right-click a folder in File Explorer -> "Sync with my other devices".
  * Other computers get shared folders automatically.

Deleted/overwritten files are never really deleted: they go to a trash folder.
Edited on two computers at once? Newest wins, the other is kept as a
"(conflict ...)" copy.

Command line (normally not needed):
  syncbox.py              open the window
  syncbox.py --install    install (shortcuts, auto-start, right-click menu)
  syncbox.py --uninstall  remove shortcuts, auto-start and right-click menu
  syncbox.py --add PATH   share a folder
Only the Python standard library is used.
"""

VERSION = 4

import fnmatch
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("APPDATA") or HOME / ".config") / "SyncBox"
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA") or HOME / ".local" / "share") / "SyncBox"
CONFIG_FILE = DATA_DIR / "config.json"
STATE_DIR = DATA_DIR / "state"
LOCAL_TRASH = DATA_DIR / "trash"
LOG_FILE = DATA_DIR / "syncbox.log"

HUB_MARKER = ".syncbox_hub"
HUB_REGISTRY = "registry.json"
HUB_FOLDERS = "folders"
HUB_TRASH = ".trash"
INSTALLER_NAME = "Install SyncBox.bat"
IPC_PORT = 47391
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PART_SUFFIX = ".syncbox-part"
SYSTEM_DRIVE = (os.environ.get("SystemDrive") or "C:").upper()

# Never synced, at any depth.
IGNORE_EVERYWHERE = [
    "~$*", "*.tmp", "*.swp", "*.part", "*" + PART_SUFFIX, ".~lock.*",
    "desktop.ini", "Thumbs.db", ".DS_Store", "$RECYCLE.BIN",
    "System Volume Information", "node_modules", "__pycache__",
]
# Skipped when sharing your whole user folder (paths relative to it).
HOME_EXCLUDES = [
    "AppData", "Application Data", "Local Settings", "NTUSER*", "ntuser*",
    "OneDrive*", "Google Drive", "Dropbox", "iCloudDrive", "Creative Cloud Files",
    "Cookies", "Recent", "SendTo", "PrintHood", "NetHood", "Templates",
    "Start Menu", "My Documents", "IntelGraphicsProfiles", "MicrosoftEdgeBackups",
    ".cache", ".vscode", ".nuget", ".gradle", ".m2", ".npm", ".conda",
    ".cargo", ".rustup", ".android", ".docker", ".ollama", ".lmstudio",
    "anaconda3", "miniconda3", "scoop", "go",
]
# Skipped when sharing a whole drive (Windows and installed programs can't be
# copied between computers - reinstall programs instead).
DRIVE_EXCLUDES = [
    "Windows", "Windows.old", "Program Files", "Program Files (x86)", "ProgramData",
    "Recovery", "PerfLogs", "Config.Msi", "$WinREAgent", "$Windows.~BT", "$Windows.~WS",
    "$SysReset", "$GetCurrent", "MSOCache", "OneDriveTemp", "Intel", "AMD", "NVIDIA",
    "Boot", "EFI", "bootmgr", "BOOTNXT", "Documents and Settings", "pagefile.sys",
    "hiberfil.sys", "swapfile.sys", "DumpStack.log*", "Users/*/AppData",
    "Users/Default*", "Users/Public/AppData", "Users/All Users",
]

INSTALL_BAT = r"""@echo off
title SyncBox installer
echo Installing SyncBox...
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0syncbox.py" --install
  goto :done
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0syncbox.py" --install
  goto :done
)
echo.
echo SyncBox needs Python. Your browser will now open the download page.
echo Install Python (tick "Add python.exe to PATH"), then double-click this file again.
start "" https://www.python.org/downloads/
pause
exit /b
:done
timeout /t 4 >nul
"""


# ============================================================ small helpers

_log_lock = threading.Lock()


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass  # pythonw has no console
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def norm(p):
    p = os.path.normcase(os.path.abspath(str(p)))
    return p if p.endswith(":\\") or p == "/" else p.rstrip("\\/")


def is_inside(child, parent):
    c, p = norm(child), norm(parent)
    return c != p and c.startswith(p if p.endswith(os.sep) else p + os.sep)


def rel_inside(child, parent):
    return Path(os.path.abspath(child)).relative_to(Path(os.path.abspath(parent))).as_posix()


def slug(name):
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()[:30] or "folder"
    return f"{s}-{uuid.uuid4().hex[:6]}"


def pythonw():
    p = Path(sys.executable).with_name("pythonw.exe")
    return p if p.exists() else Path(sys.executable)


def is_drive_root(path):
    p = os.path.abspath(path)
    return os.path.splitdrive(p)[0] != "" and p.rstrip("\\/") == os.path.splitdrive(p)[0]


def list_drives():
    if hasattr(os, "listdrives"):
        try:
            return [d.rstrip("\\").upper() for d in os.listdrives()]
        except OSError:
            pass
    return [f"{c}:" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]


# ============================================================ config

def load_config():
    cfg = {"hub": "", "hub_id": "", "token": "", "device": socket.gethostname(), "auto_receive": True,
           "folders": {}, "declined": [], "seen_close_tip": False, "paused": False}
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    write_json(CONFIG_FILE, cfg)


# ============================================================ ignore rules / local files

def make_ignore(exclude):
    everywhere = [p.lower() for p in IGNORE_EVERYWHERE]
    excl = [e.replace("\\", "/").strip("/").lower() for e in exclude if e.strip("/\\")]

    def ignored(rel):
        low = rel.lower()
        for part in low.split("/"):
            for pat in everywhere:
                if fnmatch.fnmatchcase(part, pat):
                    return True
        for e in excl:
            if low == e or low.startswith(e + "/") or fnmatch.fnmatchcase(low, e) \
                    or fnmatch.fnmatchcase(low, e + "/*"):
                return True
        return False
    return ignored


def scan(root, ignored, skip_dirs=()):
    """{relative/path: [size, mtime_ns]} for all files under root."""
    result = {}
    root = os.path.abspath(root)
    skip = {norm(d) for d in skip_dirs if d}
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        try:
            it = os.scandir(os.path.join(root, rel_dir) if rel_dir else root)
        except OSError:
            continue
        with it:
            for e in it:
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                if ignored(rel):
                    continue
                try:
                    if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                        continue
                    if e.is_dir():
                        if norm(e.path) not in skip:
                            stack.append(rel)
                        continue
                    st = os.stat(e.path)
                except OSError:
                    continue
                result[rel] = [st.st_size, st.st_mtime_ns]
    return result


def same_sig(a, b):
    if a is None or b is None:
        return a is None and b is None
    return a[0] == b[0] and a[1] == b[1]


def recently_modified(sig):
    return time.time_ns() - sig[1] < 2_000_000_000


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_sig(path):
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


def remove_empty_parents(root, folder):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            break
        folder = folder.parent


def move_to_trash(root, rel, trash_dir):
    src = Path(root) / rel
    if not src.exists():
        return
    dst = Path(trash_dir) / f"{datetime.now():%Y-%m-%d}" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst = dst.with_name(f"{dst.stem} {datetime.now():%H%M%S}{dst.suffix}")
    shutil.move(str(src), str(dst))
    remove_empty_parents(root, src.parent)


def conflict_name(rel, who):
    p = Path(rel)
    return (p.parent / f"{p.stem} (conflict {who} {datetime.now():%Y-%m-%d %H%M%S}){p.suffix}").as_posix()


# ============================================================ servers ("hubs")

def registry_apply(reg, op):
    """Same rules as the website uses."""
    fid = op["fid"]
    if op["op"] == "set":
        reg[fid] = op["entry"]
    elif op["op"] == "exclude" and fid in reg:
        ex = reg[fid].setdefault("exclude", [])
        if op["rel"] not in ex:
            ex.append(op["rel"])
    elif op["op"] == "remove":
        reg.pop(fid, None)
    return reg


def drive_is_removable(path):
    """True for USB sticks and SD cards."""
    if os.name != "nt":
        return str(path).startswith(("/media/", "/Volumes/", "/run/media/"))
    try:
        import ctypes
        root = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(root) == 2   # DRIVE_REMOVABLE
    except Exception:
        return False


def read_hub_id(path):
    try:
        text = (Path(path) / HUB_MARKER).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = re.search(r"id: ([0-9a-f]{32})", text)
    return m.group(1) if m else ""


def hub_search_roots():
    """Where to look for a server folder that moved (a USB stick with a new drive letter)."""
    if os.name == "nt":
        return [d + "\\" for d in list_drives()]
    return [p for p in ("/media", "/run/media", "/mnt", "/Volumes") if os.path.isdir(p)]


def find_hub(hub_id, old_path):
    name = Path(old_path).name
    for root in hub_search_roots():
        root = Path(root)
        cands = [root, root / name]
        try:
            level1 = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            continue
        cands += level1 + [p / name for p in level1]
        for c in cands:
            if read_hub_id(c) == hub_id:
                return str(c)
    return None


def _flush(path):
    """Make sure data is really on the USB stick before we call it safe."""
    try:
        with open(path, "rb+") as f:
            os.fsync(f.fileno())
    except OSError:
        pass


class FolderHub:
    """Server = a plain folder: a USB stick, a NAS share, a Google Drive folder..."""

    def __init__(self, path, hub_id=""):
        self.root = Path(path)
        self.hub_id = hub_id or read_hub_id(path)
        self.skip_dir = str(path)
        self.web_url = None
        self.found_at = None
        self.removable = drive_is_removable(path)
        drive = os.path.splitdrive(str(path))[0]
        self.label = f"USB stick {drive}" if self.removable and drive else str(path)

    def check(self):
        if (self.root / HUB_MARKER).is_file() and \
                (not self.hub_id or read_hub_id(self.root) == self.hub_id):
            return
        if self.hub_id:
            found = find_hub(self.hub_id, self.root)
            if found:                                  # USB stick got another drive letter
                self.__init__(found, self.hub_id)
                self.found_at = found
                return
        raise OSError(f"Server folder not found ({self.root}). If it's a USB stick, plug it in "
                      "and SyncBox continues by itself.")

    def registry(self):
        path = self.root / HUB_REGISTRY
        if not path.exists():
            return {}
        for _ in range(3):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                time.sleep(0.3)
        raise OSError("registry.json on the server is unreadable")

    def registry_op(self, op):
        reg = registry_apply(self.registry(), op)
        if op["op"] == "remove":
            src = self.root / HUB_FOLDERS / op["fid"]
            if src.exists():
                dst = self.root / HUB_TRASH / f"removed {datetime.now():%Y-%m-%d %H%M%S} {op['fid']}"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
        write_json(self.root / HUB_REGISTRY, reg)
        _flush(self.root / HUB_REGISTRY)
        return reg

    def dir(self, fid):
        d = self.root / HUB_FOLDERS / fid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def scan(self, fid):
        return scan(self.dir(fid), lambda rel: rel.endswith(PART_SUFFIX))

    def upload(self, fid, rel, src):
        dst = self.dir(fid) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + PART_SUFFIX)
        shutil.copy2(src, tmp)
        _flush(tmp)
        os.replace(tmp, dst)
        return local_sig(dst)

    def download(self, fid, rel, dst):
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + PART_SUFFIX)
        shutil.copy2(self.dir(fid) / rel, tmp)
        os.replace(tmp, dst)
        return local_sig(dst)

    def trash(self, fid, rel):
        move_to_trash(self.dir(fid), rel, self.root / HUB_TRASH / fid)

    def hash(self, fid, rel):
        return file_hash(self.dir(fid) / rel)


class HttpHub:
    """Server = the SyncBox website (syncbox_web.py on your Raspberry Pi)."""

    def __init__(self, url, token, device):
        self.url = url.rstrip("/")
        self.token = token
        self.device = device
        self.label = re.sub(r"^https?://", "", self.url)
        self.skip_dir = None
        self.web_url = self.url

    def _req(self, method, path, params=None, data=None, headers=None, timeout=60):
        url = self.url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        h = {"Authorization": f"Bearer {self.token}", "X-Device": self.device}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", "")
            except Exception:
                msg = ""
            if e.code == 401:
                raise OSError("this computer is not allowed on the server any more, reconnect "
                              "with 'Server...'")
            raise OSError(f"server error {e.code} {msg}")
        except urllib.error.URLError as e:
            raise OSError(f"can't reach {self.label} ({e.reason})")

    def _json(self, method, path, params=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        with self._req(method, path, params, data,
                       {"Content-Type": "application/json"} if data else None) as r:
            return json.loads(r.read())

    def check(self):
        self.registry()

    def registry(self):
        return self._json("GET", "/api/registry")

    def registry_op(self, op):
        return self._json("POST", "/api/registry", body=op)

    def scan(self, fid):
        return self._json("GET", "/api/scan", {"f": fid})

    def upload(self, fid, rel, src):
        st = os.stat(src)
        with open(src, "rb") as f:
            with self._req("PUT", "/api/file", {"f": fid, "p": rel, "mtime_ns": st.st_mtime_ns},
                           data=f, headers={"Content-Length": str(st.st_size)},
                           timeout=600) as r:
                return json.loads(r.read())["sig"]

    def download(self, fid, rel, dst):
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + PART_SUFFIX)
        with self._req("GET", "/api/file", {"f": fid, "p": rel}, timeout=600) as r:
            mt = r.headers.get("X-Mtime-Ns")
            with open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        if mt and mt.isdigit():
            os.utime(tmp, ns=(int(mt), int(mt)))
        os.replace(tmp, dst)
        return local_sig(dst)

    def trash(self, fid, rel):
        self._json("POST", "/api/delete", body={"f": fid, "p": rel})

    def hash(self, fid, rel):
        return self._json("GET", "/api/hash", {"f": fid, "p": rel})["sha256"]


def make_hub(cfg):
    hub = cfg.get("hub") or ""
    if hub.startswith(("http://", "https://")):
        return HttpHub(hub, cfg.get("token", ""), cfg.get("device", ""))
    return FolderHub(hub, cfg.get("hub_id", "")) if hub else None


def init_folder_hub(path):
    """Make (or update) a folder server. Returns its id, so a USB stick is
    recognised again when it gets another drive letter."""
    root = Path(path)
    (root / HUB_FOLDERS).mkdir(parents=True, exist_ok=True)
    hub_id = read_hub_id(root) or uuid.uuid4().hex
    (root / HUB_MARKER).write_text("This folder is a SyncBox server. Don't edit files in here.\n"
                                   f"id: {hub_id}\n", encoding="utf-8")
    if not (root / HUB_REGISTRY).exists():
        write_json(root / HUB_REGISTRY, {})
    publish_installer(root)
    return hub_id


def publish_installer(root):
    """Folder servers: put the installer there, so other PCs install with one click."""
    root = Path(root)
    try:
        dst = root / "syncbox.py"
        their = 0
        if dst.exists():
            m = re.search(r"^VERSION = (\d+)", dst.read_text(encoding="utf-8", errors="ignore"), re.M)
            their = int(m.group(1)) if m else 0
        if their < VERSION:
            shutil.copy2(Path(__file__).resolve(), dst)
            (root / INSTALLER_NAME).write_bytes(INSTALL_BAT.replace("\n", "\r\n").encode("utf-8"))
    except OSError as e:
        log(f"could not copy installer to server: {e}")


# ============================================================ sync engine

class Paused(Exception):
    pass


def sync_folder(fid, local, hub, exclude, device, progress=None, on_event=None):
    """Two-way sync of one folder with its copy on the server.
    Returns (changes, failed [(file, error)], pending = files still being saved)."""

    def note(what, rel):
        log(f"[{fid}] {what}: {rel}")
        if on_event:
            on_event(what, rel)

    local = Path(local)
    if not local.is_dir():
        raise Paused("folder not found on this computer")
    state_file = STATE_DIR / f"{fid}.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    ignored = make_ignore(exclude)
    loc = scan(local, ignored, skip_dirs=[hub.skip_dir, DATA_DIR, INSTALL_DIR])
    hb = {k: v for k, v in hub.scan(fid).items() if not ignored(k)}

    # Safety: one side suddenly empty while we know lots of files -> don't mass-delete.
    if len(state) >= 20 and not loc and hb:
        raise Paused("folder on this computer is suddenly empty, paused to be safe")
    if len(state) >= 20 and not hb and loc:
        raise Paused("folder on the server is suddenly empty, paused to be safe")

    local_trash = LOCAL_TRASH / fid
    changes, failed, pending = 0, [], 0
    last_save = time.time()

    for rel in sorted(set(loc) | set(hb) | set(state)):
        L, H = loc.get(rel), hb.get(rel)
        S = state.get(rel)
        local_changed = not same_sig(L, S["local"] if S else None)
        hub_changed = not same_sig(H, S["hub"] if S else None)
        if not local_changed and not hub_changed:
            continue
        if (L and local_changed and recently_modified(L)) or \
           (H and hub_changed and recently_modified(H)):
            pending += 1
            continue  # still being saved, next round
        try:
            if local_changed and not hub_changed:
                if L is None:
                    hub.trash(fid, rel)
                    state.pop(rel, None)
                    note("deleted (also on your other devices)", rel)
                else:
                    state[rel] = {"local": L, "hub": hub.upload(fid, rel, local / rel)}
                    note("sent", rel)
            elif hub_changed and not local_changed:
                if H is None:
                    move_to_trash(local, rel, local_trash)
                    state.pop(rel, None)
                    note("deleted here (deleted on another device)", rel)
                else:
                    state[rel] = {"local": hub.download(fid, rel, local / rel), "hub": H}
                    note("received", rel)
            # ---- changed on both sides ----
            elif L is None and H is None:
                state.pop(rel, None)
                continue
            elif L is None:                       # deleted here, edited elsewhere
                state[rel] = {"local": hub.download(fid, rel, local / rel), "hub": H}
                note("kept (edited on another device)", rel)
            elif H is None:                       # deleted elsewhere, edited here
                state[rel] = {"local": L, "hub": hub.upload(fid, rel, local / rel)}
                note("kept (edited here)", rel)
            elif L[0] == H[0] and file_hash(local / rel) == hub.hash(fid, rel):
                state[rel] = {"local": L, "hub": H}   # identical already
                continue
            else:                                  # real conflict: newest wins
                if L[1] >= H[1]:
                    cname = conflict_name(rel, "other device")
                    hub.download(fid, rel, local / cname)
                    state[rel] = {"local": L, "hub": hub.upload(fid, rel, local / rel)}
                else:
                    cname = conflict_name(rel, device)
                    shutil.copy2(local / rel, local / cname)
                    state[rel] = {"local": hub.download(fid, rel, local / rel), "hub": H}
                note(f"edited on two computers, other version kept as '{Path(cname).name}'", rel)
            changes += 1
            if progress and changes % 10 == 0:
                progress(changes)
            if time.time() - last_save > 30:      # big first sync: save progress
                write_json(state_file, state)
                last_save = time.time()
        except OSError as e:
            failed.append((rel, str(e)))
            if "can't reach" in str(e) or "not allowed" in str(e):
                break  # server gone: stop this round

    write_json(state_file, state)
    return changes, failed, pending


def receive_dir(cfg):
    """Where folders from other computers arrive (the user can change it)."""
    return Path(cfg.get("receive_dir") or HOME / "SyncBox")


def default_location(entry, cfg=None):
    base = receive_dir(cfg or {})
    if entry.get("drive"):
        d = entry["drive"].upper()
        return d + "\\" if os.path.isdir(d + "\\") else str(base / entry["name"])
    if entry.get("home_rel") is not None:
        return str(HOME / entry["home_rel"]) if entry["home_rel"] else str(HOME)
    return str(base / entry["name"])


class Engine(threading.Thread):
    """Background thread that keeps syncing."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.wake = threading.Event()
        self.status = {}          # fid -> text
        self.hub_status = "Starting..."
        self.safe_to_unplug = None   # None = not a USB stick, else True/False
        self.registry = {}
        self.reported = set()
        self.activity = deque(maxlen=500)   # (time, folder, what, file)
        self.cycle_lock = threading.Lock()  # held while syncing, so a folder can't move mid-sync

    def run(self):
        while True:
            start = time.time()
            try:
                with self.cycle_lock:
                    self.cycle()
            except OSError as e:
                self.hub_status = "Can't reach the server, retrying..."
                log(f"server problem: {e}")
            except Exception as e:
                log(f"error: {e!r}")
            took = time.time() - start
            self.wake.wait(max(5, took * 3))   # big folders -> check less often
            self.wake.clear()

    def event(self, folder, what, rel):
        self.activity.appendleft((datetime.now().strftime("%d-%m %H:%M:%S"), folder, what, rel))

    def cycle(self):
        cfg = self.app.snapshot()
        if cfg.get("paused"):
            self.hub_status = "Paused. Click Resume to sync again."
            self.safe_to_unplug = None
            return
        hub = make_hub(cfg)
        if not hub:
            self.hub_status = "No server chosen yet"
            return
        usb = isinstance(hub, FolderHub)
        if usb:
            try:
                hub.check()
            except OSError as e:
                self.hub_status = str(e)
                self.safe_to_unplug = None
                return
            if hub.found_at:
                self.app.set_hub_path(hub.found_at)
                log(f"server folder found at {hub.found_at}")
            usb = hub.removable
        reg = hub.registry()           # raises OSError if unreachable
        self.registry = reg

        for fid in list(cfg["folders"]):
            if fid not in reg:      # removed from all devices (website or another computer)
                self.app.detach(fid, decline=True)
                log(f"[{fid}] was removed from the server; stopped syncing (files kept)")

        if cfg["auto_receive"]:
            for fid, entry in reg.items():
                if fid in cfg["folders"] or fid in cfg["declined"]:
                    continue
                target = default_location(entry, cfg)
                if not entry.get("drive") and self.app.overlap_problem(target) is None:
                    self.app.attach(fid, target)
                    log(f"[{fid}] receiving '{entry['name']}' into {target}")
                else:
                    self.status[fid] = "Not on this computer, click 'Get it here'"

        cfg = self.app.snapshot()
        if usb:
            self.hub_status = "Syncing... don't unplug the USB stick yet"
            self.safe_to_unplug = False
        else:
            self.hub_status = "Connected"
        all_done = True
        for fid, f in cfg["folders"].items():
            entry = reg.get(fid)
            if not entry:
                continue
            self.status[fid] = "Syncing..."
            try:
                n, failed, pending = sync_folder(
                    fid, f["local"], hub, entry.get("exclude", []), cfg["device"],
                    progress=lambda c, fid=fid: self.status.__setitem__(fid, f"Syncing... {c} files done"),
                    on_event=lambda what, rel, name=entry["name"]: self.event(name, what, rel))
                for rel, err in failed:
                    if (fid, rel, err) not in self.reported:
                        self.reported.add((fid, rel, err))
                        log(f"[{fid}] couldn't sync {rel}: {err}")
                if failed:
                    self.status[fid] = f"{len(failed)} file(s) couldn't sync (open in another program?)"
                elif pending:
                    self.status[fid] = f"Waiting for {pending} file(s) to finish saving"
                else:
                    self.status[fid] = f"Up to date ({datetime.now():%H:%M})"
                all_done = all_done and not failed and not pending
            except Paused as p:
                self.status[fid] = f"Paused: {p}"
                all_done = False
            except OSError as e:
                self.status[fid] = "Problem, retrying..."
                log(f"[{fid}] {e}")
                all_done = False
        if usb:
            self.safe_to_unplug = all_done
            self.hub_status = ("Everything is on the USB stick. Safe to unplug." if all_done
                               else "Syncing... don't unplug the USB stick yet")
        else:
            self.safe_to_unplug = None


# ============================================================ Windows integration

def run_ps(script):
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                   capture_output=True, creationflags=NO_WINDOW)


def _q(s):
    return str(s).replace("'", "''")


def make_shortcut(folder_kind, name, arguments):
    """folder_kind: Desktop, Programs or Startup."""
    run_ps(
        f"$p = Join-Path ([Environment]::GetFolderPath('{folder_kind}')) '{_q(name)}';"
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        f"$s.TargetPath = '{_q(pythonw())}';"
        f"$s.Arguments = '{_q(arguments)}';"
        f"$s.WorkingDirectory = '{_q(INSTALL_DIR)}';"
        f"$s.Description = 'SyncBox: keep folders in sync';"
        f"$s.Save()")


def remove_shortcut(folder_kind, name):
    run_ps(f"Remove-Item -ErrorAction SilentlyContinue "
           f"(Join-Path ([Environment]::GetFolderPath('{folder_kind}')) '{_q(name)}')")


def startup_enabled():
    appdata = os.environ.get("APPDATA", "")
    return (Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/SyncBox.lnk").exists()


def set_startup(on):
    if os.name != "nt":
        return
    script = INSTALL_DIR / "syncbox.py"
    if on:
        make_shortcut("Startup", "SyncBox.lnk", f'"{script}" --hidden')
    else:
        remove_shortcut("Startup", "SyncBox.lnk")


# "%1." / "%V.": a drive arrives as C:\ and \" would escape the closing quote;
# the trailing dot is removed again by os.path.abspath on Windows.
MENU_KEYS = [(r"Software\Classes\Directory\shell\SyncBox", "%1"),
             (r"Software\Classes\Directory\Background\shell\SyncBox", "%V."),
             (r"Software\Classes\Drive\shell\SyncBox", "%1.")]


def register_context_menu(script):
    import winreg
    for key, arg in MENU_KEYS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Sync with my other devices (SyncBox)")
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, "imageres.dll,-5340")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + r"\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ,
                              f'"{pythonw()}" "{script}" --add "{arg}"')


def unregister_context_menu():
    import winreg
    for key, _ in MENU_KEYS:
        for sub in (key + r"\command", key):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError:
                pass


def read_server_file(cfg, folder):
    """server.txt (written by the website installer): line 1 = URL, line 2 = key."""
    f = Path(folder) / "server.txt"
    if not f.exists():
        return False
    lines = [x.strip() for x in f.read_text(encoding="utf-8", errors="ignore").splitlines() if x.strip()]
    if len(lines) >= 2:
        if norm(cfg.get("hub") or "x:/none") != norm(lines[0]):
            cfg["folders"], cfg["declined"] = {}, []
        cfg["hub"], cfg["token"] = lines[0], lines[1]
    f.unlink()
    return True


def install():
    if os.name != "nt":
        print("The installer is for Windows. On other systems just run: python3 syncbox.py")
        return
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    me = Path(__file__).resolve()
    script = INSTALL_DIR / "syncbox.py"
    if norm(me) != norm(script):
        shutil.copy2(me, script)

    cfg = load_config()
    if read_server_file(cfg, me.parent) or read_server_file(cfg, INSTALL_DIR):
        print(f"Server: {cfg['hub']}")
    elif (me.parent / HUB_MARKER).exists() and not cfg["hub"]:
        cfg["hub"] = str(me.parent)        # installed from a USB stick / folder server: connect to it
        cfg["hub_id"] = read_hub_id(me.parent)
    save_config(cfg)

    print("Creating shortcuts...")
    make_shortcut("Desktop", "SyncBox.lnk", f'"{script}"')
    make_shortcut("Programs", "SyncBox.lnk", f'"{script}"')
    set_startup(True)
    register_context_menu(script)
    send_to_running("quit")   # restart an older running copy
    time.sleep(1)
    print("Done! SyncBox is opening...")
    subprocess.Popen([str(pythonw()), str(script)], cwd=str(INSTALL_DIR),
                     creationflags=0x00000008 | NO_WINDOW, close_fds=True)


def uninstall():
    if os.name == "nt":
        for kind in ("Desktop", "Programs", "Startup"):
            remove_shortcut(kind, "SyncBox.lnk")
        unregister_context_menu()
    send_to_running("quit")
    print("SyncBox shortcuts, auto-start and right-click menu removed.\n"
          "Your files were not touched. Settings are in", DATA_DIR)


# ============================================================ single instance

def claim_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", IPC_PORT))
        s.listen(5)
        return s
    except OSError:
        s.close()
        return None


def send_to_running(msg):
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=2) as c:
            c.sendall(msg.encode("utf-8"))
        return True
    except OSError:
        return False


# ============================================================ window

class App:
    def __init__(self, server_sock, hidden=False, add_path=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.cfg = load_config()
        self.lock = threading.Lock()
        self.msgs = queue.Queue()
        self.moving = set()      # folders being moved right now

        if os.name == "nt":
            try:   # sharp text on high-DPI screens
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        self.root = tk.Tk()
        self.root.title("SyncBox")
        k = max(1.0, self.root.winfo_fpixels("1i") / 96)
        self.root.geometry(f"{int(900 * k)}x{int(500 * k)}")
        self.root.minsize(int(700 * k), int(380 * k))
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.build_ui()

        self.engine = Engine(self)
        self.engine.start()
        threading.Thread(target=self.ipc_loop, args=(server_sock,), daemon=True).start()

        if hidden:
            self.root.withdraw()
        if not self.cfg["hub"]:
            self.root.after(300, self.server_dialog)
        if add_path:
            self.root.after(800, lambda: self.add_folder(add_path))
        self.root.after(500, self.refresh)
        self.root.after(200, self.poll_msgs)

    # ---------- shared state (used by the engine thread too)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.cfg))

    def attach(self, fid, local):
        Path(local).mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.cfg["folders"][fid] = {"local": str(local)}
            if fid in self.cfg["declined"]:
                self.cfg["declined"].remove(fid)
            save_config(self.cfg)

    def detach(self, fid, decline):
        with self.lock:
            self.cfg["folders"].pop(fid, None)
            if decline and fid not in self.cfg["declined"]:
                self.cfg["declined"].append(fid)
            save_config(self.cfg)
        try:
            (STATE_DIR / f"{fid}.json").unlink()
        except OSError:
            pass

    def overlap_problem(self, path, new_exclude=(), skip_fid=None):
        """Folders may not overlap, unless the overlapping part is excluded."""
        cfg = self.snapshot()
        hub = cfg["hub"]
        if hub and not hub.startswith("http") and (norm(path) == norm(hub) or is_inside(path, hub)):
            return "That's the server folder itself."
        for fid, f in cfg["folders"].items():
            if fid == skip_fid:
                continue
            other = f["local"]
            ex = self.engine.registry.get(fid, {}).get("exclude", []) if hasattr(self, "engine") else []
            if norm(path) == norm(other):
                return f"You already sync this folder:\n{other}"
            if is_inside(path, other) and not make_ignore(ex)(rel_inside(path, other)):
                return f"It's inside a folder you already sync:\n{other}"
            if is_inside(other, path) and not make_ignore(new_exclude)(rel_inside(other, path)):
                return f"It contains a folder you already sync:\n{other}"
        return None

    # ---------- UI

    def build_ui(self):
        tk, ttk = self.tk, self.ttk
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)
        style.configure("Big.TButton", padding=(10, 6))

        top = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="SyncBox", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Button(top, text="Server...", command=self.server_dialog).pack(side="right")
        self.b_web = ttk.Button(top, text="Open website", command=self.open_website)
        self.b_web.pack(side="right", padx=(0, 6))
        self.b_pause = ttk.Button(top, text="Pause syncing", command=self.toggle_pause)
        self.b_pause.pack(side="right", padx=(0, 6))
        self.server_lbl = ttk.Label(top, text="", foreground="#555")
        self.server_lbl.pack(side="right", padx=10)

        line = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        line.pack(fill="x")
        self.status_lbl = tk.Label(line, text="", font=("Segoe UI", 11, "bold"), anchor="w",
                                   padx=10, pady=6, bg="#eef2ff", fg="#1e3a8a")
        self.status_lbl.pack(fill="x")

        bar = ttk.Frame(self.root, padding=(12, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="+  Add a folder...", style="Big.TButton",
                   command=self.add_folder).pack(side="left")
        ttk.Button(bar, text="Share my whole user folder", style="Big.TButton",
                   command=self.share_home).pack(side="left", padx=6)
        ttk.Button(bar, text="Share a whole drive...", style="Big.TButton",
                   command=self.drive_dialog).pack(side="left")
        ttk.Button(bar, text="Sync now", command=lambda: self.engine.wake.set()).pack(side="right")

        mid = ttk.Frame(self.root, padding=(12, 4))
        mid.pack(fill="both", expand=True)
        nb = ttk.Notebook(mid)
        nb.pack(fill="both", expand=True)
        ftab = ttk.Frame(nb, padding=4)
        atab = ttk.Frame(nb, padding=4)
        nb.add(ftab, text="  Shared folders  ")
        nb.add(atab, text="  Recent activity  ")
        acols = ("time", "folder", "what", "file")
        self.act = ttk.Treeview(atab, columns=acols, show="headings")
        for c, title, w in (("time", "When", 120), ("folder", "Folder", 150), ("what", "What happened", 260),
                            ("file", "File", 330)):
            self.act.heading(c, text=title, anchor="w")
            self.act.column(c, width=w, anchor="w")
        asb = ttk.Scrollbar(atab, orient="vertical", command=self.act.yview)
        self.act.configure(yscrollcommand=asb.set)
        self.act.pack(side="left", fill="both", expand=True)
        asb.pack(side="right", fill="y")
        self._act_seen = None
        cols = ("name", "where", "status")
        self.tree = ttk.Treeview(ftab, columns=cols, show="headings", selectmode="browse")
        for c, title, w in (("name", "Folder", 190), ("where", "On this computer", 330),
                            ("status", "Status", 280)):
            self.tree.heading(c, text=title, anchor="w")
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(ftab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_buttons())

        acts = ttk.Frame(self.root, padding=(12, 4))
        acts.pack(fill="x")
        self.b_open = ttk.Button(acts, text="Open", command=self.open_selected)
        self.b_move = ttk.Button(acts, text="Move...", command=self.move_folder)
        self.b_excl = ttk.Button(acts, text="Don't share a subfolder...", command=self.exclude_subfolder)
        self.b_get = ttk.Button(acts, text="Get it here...", command=self.get_here)
        self.b_stop = ttk.Button(acts, text="Stop syncing on this PC", command=self.stop_here)
        self.b_del = ttk.Button(acts, text="Remove from all devices", command=self.remove_everywhere)
        for b in (self.b_open, self.b_move, self.b_excl, self.b_get, self.b_stop, self.b_del):
            b.pack(side="left", padx=(0, 6))

        bottom = ttk.Frame(self.root, padding=(12, 6, 12, 10))
        bottom.pack(fill="x")
        self.v_auto = tk.BooleanVar(value=self.cfg["auto_receive"])
        self.v_start = tk.BooleanVar(value=startup_enabled())
        ttk.Checkbutton(bottom, text="Automatically get folders shared from my other devices",
                        variable=self.v_auto, command=self.toggle_auto).pack(anchor="w")
        recv = ttk.Frame(bottom)
        recv.pack(anchor="w", padx=(26, 0), pady=(0, 4))
        self.recv_lbl = ttk.Label(recv, foreground="#555")
        self.recv_lbl.pack(side="left")
        ttk.Button(recv, text="Change...", command=self.change_receive_dir).pack(side="left", padx=(8, 0))
        self.show_receive_dir()
        ttk.Checkbutton(bottom, text="Start SyncBox when I log in",
                        variable=self.v_start, command=self.toggle_start).pack(anchor="w")
        ttk.Label(bottom, foreground="#666",
                  text="Tip: right-click any folder or drive in File Explorer, then 'Show more options' > "
                       "'Sync with my other devices'.").pack(anchor="w", pady=(4, 0))
        ttk.Button(bottom, text="Quit SyncBox", command=self.quit).place(relx=1.0, rely=0, anchor="ne")
        self.update_buttons()

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def update_buttons(self):
        fid = self.selected()
        mine = bool(fid and fid in self.cfg["folders"])
        movable = mine and fid not in self.moving and self.can_move(self.cfg["folders"][fid]["local"])
        for b, on in ((self.b_open, mine), (self.b_move, movable), (self.b_excl, mine), (self.b_stop, mine),
                      (self.b_get, bool(fid) and not mine), (self.b_del, bool(fid))):
            b.state(["!disabled"] if on else ["disabled"])
        self.b_web.state(["!disabled"] if str(self.cfg.get("hub", "")).startswith("http")
                         else ["disabled"])

    def refresh(self):
        cfg = self.snapshot()
        hub = make_hub(cfg)
        label = hub.label if hub else "none"
        if len(label) > 40:
            label = "..." + label[-37:]
        self.server_lbl.config(text=f"Server: {label}")
        st = self.engine.hub_status
        safe = self.engine.safe_to_unplug
        bg, fg = (("#e7f8ef", "#065f46") if safe or st == "Connected" else
                  ("#fff6e5", "#92400e") if safe is False or st.startswith(("Paused", "Starting")) else
                  ("#fdecec", "#991b1b") if "not found" in st or "Can't" in st else ("#eef2ff", "#1e3a8a"))
        icon = "\u2714  " if safe or st == "Connected" else "\u23f8  " if st.startswith("Paused") else ""
        self.status_lbl.config(text=icon + st, bg=bg, fg=fg)
        self.b_pause.config(text="Resume syncing" if cfg.get("paused") else "Pause syncing")
        acts = list(self.engine.activity)
        if (len(acts), acts[0] if acts else None) != self._act_seen:
            self._act_seen = (len(acts), acts[0] if acts else None)
            self.act.delete(*self.act.get_children())
            for row in acts[:300]:
                self.act.insert("", "end", values=row)
        reg = self.engine.registry
        rows = {}
        for fid, entry in reg.items():
            if fid in cfg["folders"]:
                where = cfg["folders"][fid]["local"]
                status = self.engine.status.get(fid, "Waiting...")
            else:
                where = "(not on this computer)"
                status = self.engine.status.get(fid, "Click 'Get it here' to download")
            name = entry["name"]
            if entry.get("added_by") and entry.get("added_by") != cfg["device"]:
                name += f"  (from {entry['added_by']})"
            rows[fid] = (name, where, status)
        for fid, f in cfg["folders"].items():
            if fid not in rows:
                rows[fid] = (Path(f["local"]).name, f["local"], self.engine.status.get(fid, "Waiting..."))
        existing = set(self.tree.get_children())
        for fid in existing - set(rows):
            self.tree.delete(fid)
        for fid, vals in rows.items():
            if fid in existing:
                self.tree.item(fid, values=vals)
            else:
                self.tree.insert("", "end", iid=fid, values=vals)
        self.update_buttons()
        self.root.after(1000, self.refresh)

    # ---------- actions

    def need_server(self):
        from tkinter import messagebox
        hub = make_hub(self.snapshot())
        try:
            if not hub:
                raise OSError("no server")
            hub.check()
            return hub
        except OSError as e:
            messagebox.showwarning("SyncBox", f"Can't reach your server right now.\n\n{e}\n\n"
                                              "Check your connection, or use 'Server...'.")
            return None

    def add_folder(self, path=None, kind=None):
        """kind: None (normal folder), 'home' or 'drive'."""
        from tkinter import filedialog, messagebox
        self.show()
        hub = self.need_server()
        if not hub:
            return False
        if not path:
            path = filedialog.askdirectory(title="Choose a folder to sync with your other devices",
                                           initialdir=str(HOME))
            if not path:
                return False
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            messagebox.showerror("SyncBox", f"Not a folder:\n{path}")
            return False
        if is_drive_root(path):
            kind = "drive"
        elif norm(path) == norm(HOME):
            kind = "home"

        if kind == "drive":
            letter = os.path.splitdrive(path)[0].upper()
            path = letter + "\\"
            exclude = list(DRIVE_EXCLUDES)
            if letter == SYSTEM_DRIVE:
                exclude.append("Users")   # your user folder is shared separately (see below)
            name = f"Drive {letter}"
            entry = {"name": name, "drive": letter, "home_rel": None}
        elif kind == "home":
            exclude = list(HOME_EXCLUDES)
            entry = {"name": f"User folder ({HOME.name})", "home_rel": ""}
        else:
            exclude = []
            entry = {"name": Path(path).name,
                     "home_rel": rel_inside(path, HOME) if is_inside(path, HOME) else None}

        problem = self.overlap_problem(path, exclude)
        if problem:
            messagebox.showerror("SyncBox", f"Can't add this folder.\n\n{problem}")
            return False
        fid = slug(entry["name"])
        entry.update({"exclude": exclude, "added_by": self.cfg["device"],
                      "added_at": datetime.now().isoformat(timespec="seconds")})
        try:
            self.engine.registry = hub.registry_op({"op": "set", "fid": fid, "entry": entry})
        except OSError as e:
            messagebox.showerror("SyncBox", f"Couldn't reach the server:\n{e}")
            return False
        self.attach(fid, path)
        self.engine.status[fid] = "Starting first sync..."
        self.engine.wake.set()
        log(f"[{fid}] now sharing {path}")
        return True

    def share_home(self):
        from tkinter import messagebox
        if messagebox.askyesno(
                "Share my whole user folder",
                f"This shares everything in\n{HOME}\n(Desktop, Documents, Pictures, Downloads, ...)\n\n"
                "Program settings and caches (like AppData) and OneDrive folders are skipped.\n"
                "Afterwards you can pick subfolders you DON'T want shared with "
                "\"Don't share a subfolder...\".\n\n"
                "The first sync can take a long time if you have a lot of files. Continue?"):
            self.add_folder(str(HOME), kind="home")

    def drive_dialog(self):
        from tkinter import messagebox
        tk, ttk = self.tk, self.ttk
        d = tk.Toplevel(self.root)
        d.title("Share a whole drive")
        d.transient(self.root)
        d.grab_set()
        f = ttk.Frame(d, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Which drive do you want to share?",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(f, foreground="#555", justify="left", wraplength=460,
                  text="Everything on the drive is shared, except Windows itself and installed "
                       "programs (those can't be copied to another computer, install them there "
                       "instead). For your system drive, your user folder is shared too.").pack(
            anchor="w", pady=(2, 10))

        def pick(letter):
            d.destroy()
            msg = (f"Share everything on drive {letter}?\n\n"
                   "Skipped: Windows, Program Files, ProgramData and other system folders.\n")
            if letter == SYSTEM_DRIVE:
                msg += f"Your user folder ({HOME}) is shared as well, without AppData.\n"
            msg += ("\nYour other computers get it when you click 'Get it here' there.\n"
                    "The first sync can take hours if the drive is big. Continue?")
            if not messagebox.askyesno("SyncBox", msg):
                return
            if self.add_folder(letter + "\\", kind="drive") and letter == SYSTEM_DRIVE:
                if not any(norm(x["local"]) == norm(HOME) for x in self.snapshot()["folders"].values()):
                    self.add_folder(str(HOME), kind="home")

        for letter in list_drives():
            try:
                u = shutil.disk_usage(letter + "\\")
                info = f"{(u.total - u.free) / 1e9:.0f} GB used of {u.total / 1e9:.0f} GB"
            except OSError:
                continue
            extra = "  (Windows drive)" if letter == SYSTEM_DRIVE else ""
            ttk.Button(f, text=f"Drive {letter}{extra}    {info}",
                       command=lambda l=letter: pick(l)).pack(fill="x", pady=2)
        ttk.Button(f, text="Cancel", command=d.destroy).pack(anchor="e", pady=(10, 0))

    def exclude_subfolder(self):
        from tkinter import filedialog, messagebox
        fid = self.selected()
        if not fid or fid not in self.cfg["folders"]:
            return
        hub = self.need_server()
        if not hub:
            return
        local = self.cfg["folders"][fid]["local"]
        sub = filedialog.askdirectory(title="Choose a subfolder that should NOT be shared",
                                      initialdir=local)
        if not sub:
            return
        if not is_inside(sub, local):
            messagebox.showerror("SyncBox", f"That folder is not inside\n{local}")
            return
        rel = rel_inside(sub, local)
        self.engine.registry = hub.registry_op({"op": "exclude", "fid": fid, "rel": rel})
        messagebox.showinfo("SyncBox", f"'{rel}' is no longer shared (on all your devices).\n"
                                       "Files already copied to other devices stay there.")
        self.engine.wake.set()

    def get_here(self):
        from tkinter import filedialog, messagebox
        fid = self.selected()
        entry = self.engine.registry.get(fid or "")
        if not entry:
            return
        default = default_location(entry, self.snapshot())
        ans = messagebox.askyesnocancel("SyncBox", f"Put '{entry['name']}' here?\n\n{default}\n\n"
                                                   "Yes = use this place\nNo = choose another folder")
        if ans is None:
            return
        target = default if ans else filedialog.askdirectory(
            title=f"Where should '{entry['name']}' go?", initialdir=str(HOME))
        if not target:
            return
        problem = self.overlap_problem(target, entry.get("exclude", []))
        if problem:
            messagebox.showerror("SyncBox", f"Can't put it there.\n\n{problem}")
            return
        self.attach(fid, target)
        self.engine.wake.set()

    def stop_here(self):
        from tkinter import messagebox
        fid = self.selected()
        if fid and messagebox.askyesno("SyncBox", "Stop syncing this folder on THIS computer?\n\n"
                                                  "Your files stay where they are, and the folder "
                                                  "keeps syncing on your other devices."):
            self.detach(fid, decline=True)

    def remove_everywhere(self):
        from tkinter import messagebox
        fid = self.selected()
        if not fid:
            return
        hub = self.need_server()
        if not hub:
            return
        if not messagebox.askyesno("SyncBox", "Stop syncing this folder on ALL your devices?\n\n"
                                              "Files on your computers are NOT deleted, they just "
                                              "stop syncing. The copy on the server goes to its "
                                              "trash."):
            return
        self.engine.registry = hub.registry_op({"op": "remove", "fid": fid})
        self.detach(fid, decline=True)

    def open_selected(self):
        fid = self.selected()
        if fid and fid in self.cfg["folders"] and hasattr(os, "startfile"):
            os.startfile(self.cfg["folders"][fid]["local"])

    def open_website(self):
        import webbrowser
        hub = make_hub(self.snapshot())
        if hub and hub.web_url:
            webbrowser.open(hub.web_url)

    def toggle_auto(self):
        with self.lock:
            self.cfg["auto_receive"] = self.v_auto.get()
            save_config(self.cfg)
        self.engine.wake.set()

    def show_receive_dir(self):
        self.recv_lbl.config(text=f"New folders arrive in:  {receive_dir(self.snapshot())}")

    def change_receive_dir(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Where should folders from your other computers arrive?",
                                       initialdir=str(receive_dir(self.snapshot())))
        if not path:
            return
        with self.lock:
            self.cfg["receive_dir"] = os.path.normpath(path)
            save_config(self.cfg)
        self.show_receive_dir()

    @staticmethod
    def can_move(local):
        """Whole drives and the user folder itself can't be moved."""
        p = Path(local)
        return p.parent != p and norm(local) != norm(HOME)

    def move_folder(self):
        from tkinter import filedialog, messagebox
        fid = self.selected()
        if not fid or fid not in self.cfg["folders"] or fid in self.moving:
            return
        local = self.cfg["folders"][fid]["local"]
        if not self.can_move(local):
            messagebox.showerror("SyncBox", "A whole drive or your user folder can't be moved.")
            return
        parent = filedialog.askdirectory(title=f"Move '{Path(local).name}' into which folder?",
                                         initialdir=str(Path(local).parent))
        if not parent:
            return
        target = os.path.normpath(os.path.join(parent, Path(local).name))
        if norm(target) == norm(local):
            return
        if os.path.exists(target):
            messagebox.showerror("SyncBox", f"There's already a folder called '{Path(local).name}' there:\n"
                                            f"{target}\n\nChoose another place.")
            return
        if is_inside(target, local):
            messagebox.showerror("SyncBox", "You can't move a folder into itself.")
            return
        ex = self.engine.registry.get(fid, {}).get("exclude", [])
        problem = self.overlap_problem(target, ex, skip_fid=fid)
        if problem:
            messagebox.showerror("SyncBox", f"Can't move it there.\n\n{problem}")
            return
        if not messagebox.askyesno("SyncBox", f"Move this folder and everything in it?\n\nFrom:  {local}\n"
                                              f"To:      {target}\n\nIt keeps syncing from the new place. "
                                              "Close any files from it that are open first."):
            return
        self.moving.add(fid)
        self.engine.status[fid] = "Moving..."
        threading.Thread(target=self._move_worker, args=(fid, local, target), daemon=True).start()

    def _move_worker(self, fid, local, target):
        def use_target():
            with self.lock:
                if fid in self.cfg["folders"]:
                    self.cfg["folders"][fid]["local"] = target
                    save_config(self.cfg)
        try:
            with self.engine.cycle_lock:          # wait until the current sync round is done
                self.engine.status[fid] = "Moving..."
                if os.path.splitdrive(local)[0].lower() == os.path.splitdrive(target)[0].lower():
                    os.rename(local, target)      # same drive: instant, all or nothing
                    use_target()
                    leftovers = False
                else:
                    # other drive: first a complete copy, then switch, then clean up the old place.
                    # If the copy fails, nothing changes and the old folder keeps syncing.
                    shutil.copytree(local, target, symlinks=True)
                    use_target()
                    shutil.rmtree(local, ignore_errors=True)
                    leftovers = os.path.exists(local)
            log(f"[{fid}] moved from {local} to {target}")
            msg = f"Done! The folder is now in:\n{target}"
            if leftovers:
                msg += f"\n\nSome files in the old place couldn't be removed (still open?):\n{local}\n" \
                       "They're no longer synced, so you can delete them yourself."
            self.msgs.put("info\n" + msg)
        except OSError as e:
            log(f"[{fid}] move failed: {e}")
            msg = f"Couldn't move the folder, so it stays where it was:\n{local}\n\n{e}"
            if os.path.exists(target):
                msg += f"\n\nA partial copy is in {target}. You can delete it."
            self.msgs.put("error\n" + msg)
        finally:
            self.moving.discard(fid)
            self.engine.status[fid] = "Waiting..."
            self.engine.wake.set()

    def toggle_pause(self):
        with self.lock:
            self.cfg["paused"] = not self.cfg.get("paused")
            save_config(self.cfg)
        log("syncing paused" if self.cfg["paused"] else "syncing resumed")
        self.engine.wake.set()

    def set_hub_path(self, path):
        """The USB stick showed up under another drive letter."""
        with self.lock:
            self.cfg["hub"] = str(path)
            save_config(self.cfg)

    def toggle_start(self):
        set_startup(self.v_start.get())

    def server_dialog(self):
        from tkinter import filedialog, messagebox
        tk, ttk = self.tk, self.ttk
        self.show()
        d = tk.Toplevel(self.root)
        d.title("Connect your computers")
        d.transient(self.root)
        d.grab_set()
        f = ttk.Frame(d, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, font=("Segoe UI", 12, "bold"),
                  text="How should your computers sync?").pack(anchor="w")
        hub_now = self.cfg.get("hub") or ""
        kind = tk.StringVar(value="folder" if hub_now and not hub_now.startswith("http") else "web")
        web = ttk.Frame(f)
        usb = ttk.Frame(f)

        def switch():
            (web if kind.get() == "web" else usb).pack(fill="x", padx=(26, 0), pady=(2, 8),
                                                        after=r1 if kind.get() == "web" else r2)
            (usb if kind.get() == "web" else web).pack_forget()

        r1 = ttk.Radiobutton(f, variable=kind, value="web", command=switch,
                             text="Through my SyncBox website  (Raspberry Pi or SyncBox Cloud)")
        r1.pack(anchor="w", pady=(10, 0))
        ttk.Label(web, text="Website address:").pack(anchor="w")
        v_url = tk.StringVar(value=hub_now if hub_now.startswith("http") else "http://raspberrypi.local")
        ttk.Entry(web, textvariable=v_url, width=48).pack(anchor="w")
        ttk.Label(web, text="Website password:").pack(anchor="w", pady=(6, 0))
        v_pw = tk.StringVar()
        ttk.Entry(web, textvariable=v_pw, show="*", width=30).pack(anchor="w")

        r2 = ttk.Radiobutton(f, variable=kind, value="folder", command=switch,
                             text="Through a USB stick  (or a NAS / shared folder, no website needed)")
        r2.pack(anchor="w", pady=(10, 0))
        ttk.Label(usb, foreground="#555", justify="left", wraplength=470,
                  text="Plug in your USB stick and choose it below. SyncBox makes a 'SyncBox' folder "
                       "on it. Use the same stick on every computer. It doesn't matter if it gets "
                       "another drive letter there.").pack(anchor="w")
        row = ttk.Frame(usb)
        row.pack(anchor="w", fill="x", pady=(6, 0))
        v_dir = tk.StringVar(value="" if hub_now.startswith("http") else hub_now)
        ttk.Entry(row, textvariable=v_dir, width=40).pack(side="left")
        ttk.Button(row, text="Choose...", command=lambda: v_dir.set(
            filedialog.askdirectory(parent=d, title="Choose your USB stick (or a folder on it)")
            or v_dir.get())).pack(side="left", padx=6)
        sticks = [x for x in list_drives() if drive_is_removable(x + "\\")]
        if sticks:
            ttk.Label(usb, foreground="#555", text="USB sticks found: " + ", ".join(sticks)).pack(
                anchor="w", pady=(4, 0))
            if not v_dir.get():
                v_dir.set(sticks[0] + "\\")

        ttk.Label(f, text="Name of this computer:").pack(anchor="w", pady=(12, 0))
        v_dev = tk.StringVar(value=self.cfg["device"])
        ttk.Entry(f, textvariable=v_dev, width=30).pack(anchor="w")
        switch()

        def ok():
            device = v_dev.get().strip() or socket.gethostname()
            token, hub_id = self.cfg.get("token", ""), ""
            if kind.get() == "web":
                hub = v_url.get().strip().rstrip("/")
                if not hub.startswith("http"):
                    hub = "http://" + hub
                if v_pw.get() or hub != self.cfg.get("hub"):
                    try:
                        body = json.dumps({"password": v_pw.get(), "device": device}).encode()
                        req = urllib.request.Request(hub + "/api/device-token", data=body,
                                                     headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=15) as r:
                            token = json.loads(r.read())["token"]
                    except urllib.error.HTTPError as ex:
                        msg = "Wrong password." if ex.code == 403 else \
                            "Use 'Download SyncBox' in your dashboard instead." if ex.code == 400 else \
                            f"Server error {ex.code}"
                        messagebox.showerror("SyncBox", msg, parent=d)
                        return
                    except (urllib.error.URLError, OSError, ValueError, KeyError) as ex:
                        messagebox.showerror("SyncBox", f"Can't reach {hub}\n\n{ex}\n\nIs this "
                                             "computer on the same network as the Pi?", parent=d)
                        return
            else:
                hub = v_dir.get().strip().strip('"')
                if not hub or not os.path.isdir(hub):
                    messagebox.showerror("SyncBox", "Choose your USB stick or a folder first.", parent=d)
                    return
                hub = os.path.abspath(hub)
                if is_drive_root(hub):          # whole stick chosen: use a SyncBox folder on it
                    hub = os.path.join(hub, "SyncBox")
                elif not (Path(hub) / HUB_MARKER).exists() and (Path(hub) / "SyncBox" / HUB_MARKER).exists():
                    hub = os.path.join(hub, "SyncBox")
                if not (Path(hub) / HUB_MARKER).exists() and Path(hub).exists() and any(Path(hub).iterdir()):
                    if not messagebox.askyesno("SyncBox", f"{hub}\nalready has other files in it.\n\n"
                                                          "Use it as your SyncBox server anyway?", parent=d):
                        return
                try:
                    hub_id = init_folder_hub(hub)
                except OSError as ex:
                    messagebox.showerror("SyncBox", f"Can't write there:\n{ex}", parent=d)
                    return
            with self.lock:
                if norm(self.cfg["hub"] or "x:/none") != norm(hub):
                    self.cfg["folders"], self.cfg["declined"] = {}, []   # new server, start fresh
                self.cfg.update({"hub": hub, "token": token, "device": device, "hub_id": hub_id})
                save_config(self.cfg)
            d.destroy()
            self.engine.wake.set()
            extra = ("\n\nOn your other computer: plug in the stick, open it in File Explorer and "
                     "double-click 'Install SyncBox.bat' in the SyncBox folder."
                     if kind.get() == "folder" else "")
            messagebox.showinfo("SyncBox", "Connected!\n\nNow pick folders to share, or wait a moment "
                                           "for folders from your other computers to arrive." + extra)

        b = ttk.Frame(f)
        b.pack(anchor="e", pady=(16, 0))
        ttk.Button(b, text="Connect", command=ok).pack(side="right")
        ttk.Button(b, text="Cancel", command=d.destroy).pack(side="right", padx=6)

    # ---------- window / ipc

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))

    def hide(self):
        from tkinter import messagebox
        if not self.cfg.get("seen_close_tip"):
            messagebox.showinfo("SyncBox", "SyncBox keeps syncing in the background.\n"
                                           "Open the window again with the SyncBox icon on your desktop.")
            with self.lock:
                self.cfg["seen_close_tip"] = True
                save_config(self.cfg)
        self.root.withdraw()

    def quit(self):
        self.root.destroy()
        os._exit(0)

    def ipc_loop(self, sock):
        while True:
            try:
                conn, _ = sock.accept()
                with conn:
                    data = conn.recv(65536).decode("utf-8", "replace")
                self.msgs.put(data)
            except OSError:
                time.sleep(1)

    def poll_msgs(self):
        while not self.msgs.empty():
            m = self.msgs.get()
            if m.startswith("add\n"):
                self.add_folder(m[4:].strip().strip('"'))
            elif m == "show":
                self.show()
            elif m.startswith(("info\n", "error\n")):
                from tkinter import messagebox
                kind, _, text = m.partition("\n")
                (messagebox.showinfo if kind == "info" else messagebox.showerror)("SyncBox", text)
            elif m == "quit":
                self.quit()
        self.root.after(200, self.poll_msgs)

    def run(self):
        self.root.mainloop()


# ============================================================ main

def main():
    args = sys.argv[1:]
    if "--install" in args:
        install()
        return
    if "--uninstall" in args:
        uninstall()
        return
    add_path = None
    if "--add" in args and args.index("--add") + 1 < len(args):
        add_path = args[args.index("--add") + 1]
    hidden = "--hidden" in args

    sock = claim_instance()
    if sock is None:   # already running: hand over and exit
        if add_path:
            send_to_running("add\n" + add_path)
        elif not hidden:
            send_to_running("show")
        return
    log(f"SyncBox v{VERSION} started")
    App(sock, hidden=hidden, add_path=add_path).run()


if __name__ == "__main__":
    main()
