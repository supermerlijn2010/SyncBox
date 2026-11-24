# SPDX-License-Identifier: AGPL-3.0-or-later
"""
filesync.py - automatic two-way folder sync between your devices.

Every device keeps its own normal folder (the "local" folder). All devices
share one "hub" folder that every device can reach, for example:
  - a folder inside OneDrive / Google Drive / Dropbox
  - a shared folder on a NAS or another always-on computer (\\\\server\\share)
  - a USB stick

The program watches your local folder and the hub. When you change a file
locally it is copied to the hub; when a file in the hub is newer (because
your other device changed it) it is copied to your local folder.
No pushing or pulling: it just runs in the background.

Usage:
  python filesync.py setup            # choose folders (once per device)
  python filesync.py                  # run (keeps syncing until closed)
  python filesync.py once             # sync one time and exit
  python filesync.py install-startup  # start automatically when Windows starts
  python filesync.py remove-startup   # undo install-startup

Only the Python standard library is used.
"""

import fnmatch
import hashlib
import json
import os
import shutil
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

# Config + log live per-device (not next to this script), so the script itself
# can sit in a synced folder without devices overwriting each other's settings.
APP_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "filesync"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "filesync.log"

STATE_NAME = ".filesync_state.json"   # kept in the local folder
TRASH_NAME = ".filesync_trash"        # deleted / replaced files go here (both sides)
DEFAULT_IGNORE = [
    STATE_NAME, TRASH_NAME, "~$*", "*.tmp", "*.swp", "*.part",
    ".DS_Store", "Thumbs.db", "desktop.ini",
]


# ----------------------------------------------------------------- helpers

def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass  # pythonw has no console
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    if not CONFIG_FILE.exists():
        print("No config yet. Run:  python filesync.py setup")
        sys.exit(1)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg.setdefault("interval_seconds", 5)
    cfg.setdefault("ignore", [])
    cfg.setdefault("device_name", socket.gethostname())
    return cfg


def is_ignored(rel, patterns):
    parts = Path(rel).parts
    for p in patterns:
        if any(fnmatch.fnmatch(part, p) for part in parts):
            return True
    return False


def scan(root, patterns):
    """Return {relative_path: [size, mtime_ns]} for every file under root."""
    result = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames
                       if not is_ignored(str(rel_dir / d), patterns)]
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if is_ignored(rel, patterns):
                continue
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            result[rel] = [st.st_size, st.st_mtime_ns]
    return result


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def same_sig(a, b):
    """Compare [size, mtime_ns] (we always record what's actually on disk)."""
    if a is None or b is None:
        return a is None and b is None
    return a[0] == b[0] and a[1] == b[1]


def file_is_stable(path, sig):
    """Skip files that changed in the last 2 seconds (still being written)."""
    return time.time_ns() - sig[1] > 2_000_000_000


def copy_file(src_root, dst_root, rel):
    src = Path(src_root) / rel
    dst = Path(dst_root) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".filesync.part")
    shutil.copy2(src, tmp)          # copy2 keeps the modified time
    os.replace(tmp, dst)            # atomic swap, never a half-written file
    st = os.stat(dst)
    return [st.st_size, st.st_mtime_ns]


def move_to_trash(root, rel):
    """Never delete for real: move into .filesync_trash/<date>/..."""
    src = Path(root) / rel
    if not src.exists():
        return
    dst = Path(root) / TRASH_NAME / f"{datetime.now():%Y-%m-%d}" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst = dst.with_name(f"{dst.stem}_{datetime.now():%H%M%S}{dst.suffix}")
    shutil.move(str(src), str(dst))
    remove_empty_parents(root, src.parent)


def remove_empty_parents(root, folder):
    root = Path(root).resolve()
    folder = Path(folder).resolve()
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()          # only succeeds if empty
        except OSError:
            break
        folder = folder.parent


def conflict_name(rel, device):
    p = Path(rel)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    return (p.parent / f"{p.stem} (conflict {device} {stamp}){p.suffix}").as_posix()


# ----------------------------------------------------------------- sync

def sync_once(cfg):
    local = Path(cfg["local_folder"])
    hub = Path(cfg["hub_folder"])
    device = cfg["device_name"]
    patterns = DEFAULT_IGNORE + cfg["ignore"] + ["*.filesync.part"]

    if not local.is_dir():
        log(f"Local folder not found: {local}")
        return
    if not hub.is_dir():
        log(f"Hub folder not reachable right now: {hub} (will retry)")
        return

    state_file = local / STATE_NAME
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    # state[rel] = {"local": [size, mtime], "hub": [size, mtime]}
    # = what both sides looked like the last time they were in sync.

    loc = scan(local, patterns)
    hb = scan(hub, patterns)
    changes = 0

    for rel in sorted(set(loc) | set(hb) | set(state)):
        L, H = loc.get(rel), hb.get(rel)
        S = state.get(rel)
        SL = S["local"] if S else None
        SH = S["hub"] if S else None
        local_changed = not same_sig(L, SL)
        hub_changed = not same_sig(H, SH)

        # Wait until a file is finished being written before touching it.
        if (L and local_changed and not file_is_stable(local / rel, L)) or \
           (H and hub_changed and not file_is_stable(hub / rel, H)):
            continue

        try:
            if not local_changed and not hub_changed:
                continue

            if local_changed and not hub_changed:
                if L is None:                                   # deleted here
                    move_to_trash(hub, rel)
                    state.pop(rel, None)
                    log(f"deleted  -> hub   {rel}")
                else:                                           # edited here
                    newH = copy_file(local, hub, rel)
                    state[rel] = {"local": L, "hub": newH}
                    log(f"upload   -> hub   {rel}")
                changes += 1
                continue

            if hub_changed and not local_changed:
                if H is None:                                   # deleted elsewhere
                    move_to_trash(local, rel)
                    state.pop(rel, None)
                    log(f"deleted  <- hub   {rel}")
                else:                                           # edited elsewhere
                    newL = copy_file(hub, local, rel)
                    state[rel] = {"local": newL, "hub": H}
                    log(f"download <- hub   {rel}")
                changes += 1
                continue

            # ---- both sides changed since last sync ----
            if L is None and H is None:
                state.pop(rel, None)
                continue
            if L is None:                    # deleted here, edited elsewhere: keep edit
                newL = copy_file(hub, local, rel)
                state[rel] = {"local": newL, "hub": H}
                log(f"restore  <- hub   {rel}")
                changes += 1
                continue
            if H is None:                    # deleted elsewhere, edited here: keep edit
                newH = copy_file(local, hub, rel)
                state[rel] = {"local": L, "hub": newH}
                log(f"restore  -> hub   {rel}")
                changes += 1
                continue

            # Both exist and both changed. Maybe they're identical anyway.
            if L[0] == H[0] and file_hash(local / rel) == file_hash(hub / rel):
                state[rel] = {"local": L, "hub": H}
                continue

            # Real conflict: newest wins, the other one is kept as a copy.
            if L[1] >= H[1]:
                cname = conflict_name(rel, "hub")
                shutil.copy2(hub / rel, local / cname)
                newH = copy_file(local, hub, rel)
                state[rel] = {"local": L, "hub": newH}
            else:
                cname = conflict_name(rel, device)
                shutil.copy2(local / rel, local / cname)
                newL = copy_file(hub, local, rel)
                state[rel] = {"local": newL, "hub": H}
            log(f"CONFLICT {rel}  (other version saved as '{cname}')")
            changes += 1

        except (OSError, shutil.Error) as e:
            # File locked/open in another program etc. Try again next round.
            log(f"skip (will retry) {rel}: {e}")

    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=0), encoding="utf-8")
    os.replace(tmp, state_file)
    try:  # hide the state file on Windows
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(state_file), 0x02)
    except Exception:
        pass
    return changes


def run_forever(cfg):
    log(f"filesync started on '{cfg['device_name']}'")
    log(f"  local: {cfg['local_folder']}")
    log(f"  hub:   {cfg['hub_folder']}")
    while True:
        try:
            sync_once(cfg)
        except Exception as e:  # never die in the background
            log(f"error: {e!r}")
        time.sleep(cfg["interval_seconds"])


# ----------------------------------------------------------------- setup

def setup():
    print("=== filesync setup ===\n")
    print("1) LOCAL folder: the folder on THIS computer you want to edit in.")
    local = input("   Local folder path: ").strip().strip('"')
    print("\n2) HUB folder: a folder ALL your devices can reach (OneDrive/Google")
    print("   Drive/Dropbox folder, a network share, or a USB stick).")
    print("   Use the SAME hub on every device.")
    hub = input("   Hub folder path: ").strip().strip('"')
    name = input(f"\n3) Name for this device [{socket.gethostname()}]: ").strip() \
        or socket.gethostname()

    for p in (local, hub):
        Path(p).mkdir(parents=True, exist_ok=True)
    if Path(local).resolve() == Path(hub).resolve():
        print("Local and hub must be different folders.")
        sys.exit(1)

    cfg = {"local_folder": local, "hub_folder": hub, "device_name": name,
           "interval_seconds": 5, "ignore": []}
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"\nSaved {CONFIG_FILE}")
    print("Now run:  python filesync.py              (to start syncing)")
    print("     or:  python filesync.py install-startup  (auto-start with Windows)")


def startup_script_path():
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/filesync.vbs"


def install_startup():
    if os.name != "nt":
        print("Auto-start install is Windows only. On Mac/Linux add\n"
              f"  python3 '{Path(__file__).resolve()}'\nto your login items / crontab @reboot.")
        return
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.exists():
        pyw = Path(sys.executable)
    script = Path(__file__).resolve()
    vbs = startup_script_path()
    vbs.write_text(
        'CreateObject("WScript.Shell").Run """{}"" ""{}""", 0, False\r\n'.format(pyw, script),
        encoding="utf-8")
    print(f"Installed: {vbs}")
    print("filesync will now start silently every time you log in.")
    print(f"Check what it did in: {LOG_FILE}")


def remove_startup():
    vbs = startup_script_path()
    if vbs.exists():
        vbs.unlink()
        print("Removed auto-start.")
    else:
        print("Auto-start was not installed.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "setup":
        setup()
    elif cmd == "once":
        n = sync_once(load_config())
        log(f"done ({n or 0} changes)")
    elif cmd == "install-startup":
        install_startup()
    elif cmd == "remove-startup":
        remove_startup()
    elif cmd == "run":
        run_forever(load_config())
    else:
        print(__doc__)
