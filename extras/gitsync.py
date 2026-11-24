# SPDX-License-Identifier: AGPL-3.0-or-later
"""
gitsync.py - a folder that syncs itself through GitHub. No manual push/pull.

Runs in the background. Every few seconds it:
  1. commits any changes you made in the folder
  2. pulls changes from your other devices
  3. pushes your changes to GitHub

If the same file was changed on two devices, your version stays and the other
version is saved next to it as "name (conflict <device> <time>).ext".
Every old version is also kept in the git history on GitHub.

Usage:
  python gitsync.py setup            # once per device: pick folder + GitHub repo
  python gitsync.py                  # run (keeps syncing until closed)
  python gitsync.py once             # sync one time and exit
  python gitsync.py install-startup  # start automatically when you log in
  python gitsync.py remove-startup   # undo install-startup

Needs: Git for Windows (https://git-scm.com) and Python 3. Standard library only.
"""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Config + log are stored per device, not next to this script.
APP_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "gitsync"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "gitsync.log"

DEFAULT_GITIGNORE = """# gitsync: files that are never synced
~$*
*.tmp
*.swp
Thumbs.db
desktop.ini
.DS_Store
"""

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # don't flash console windows


class GitError(Exception):
    pass


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


def git(folder, *args, check=True, binary=False):
    env = dict(os.environ)
    if not sys.stdin or not sys.stdin.isatty():
        env["GIT_TERMINAL_PROMPT"] = "0"  # never hang waiting for typing in the background
    r = subprocess.run(["git", "-C", str(folder), *args], capture_output=True,
                       env=env, creationflags=NO_WINDOW)
    out = r.stdout if binary else r.stdout.decode("utf-8", "replace").strip()
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return out


def git_ok(folder, *args):
    r = subprocess.run(["git", "-C", str(folder), *args], capture_output=True,
                       creationflags=NO_WINDOW)
    return r.returncode == 0


def load_config():
    if not CONFIG_FILE.exists():
        print("No config yet. Run:  python gitsync.py setup")
        sys.exit(1)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg.setdefault("commit_after_seconds", 5)   # wait this long after your last edit
    cfg.setdefault("pull_every_seconds", 30)
    cfg.setdefault("device_name", socket.gethostname())
    cfg.setdefault("branch", "main")
    return cfg


def newest_change_age(folder, porcelain):
    """Seconds since the most recently modified changed file."""
    newest = 0
    for line in porcelain.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        try:
            newest = max(newest, os.stat(Path(folder) / path).st_mtime)
        except OSError:
            pass
    return time.time() - newest if newest else 999


def conflict_name(path, device):
    p = Path(path)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    return (p.parent / f"{p.stem} (conflict {device} {stamp}){p.suffix}").as_posix()


# ----------------------------------------------------------------- sync

def commit_local(folder, device):
    status = git(folder, "status", "--porcelain")
    if not status:
        return False
    git(folder, "add", "-A")
    if not git(folder, "diff", "--cached", "--name-only"):
        return False
    n = len(status.splitlines())
    git(folder, "commit", "-q", "-m",
        f"{device}: {n} file(s) changed {datetime.now():%Y-%m-%d %H:%M}")
    log(f"committed {n} change(s)")
    return True


def resolve_conflicts(folder, device):
    """Keep our version, save theirs as a conflict copy, finish the merge."""
    files = git(folder, "diff", "--name-only", "--diff-filter=U").splitlines()
    for f in files:
        stages = git(folder, "ls-files", "-u", "--", f)
        have = {line.split()[2] for line in stages.splitlines()}  # '1','2','3'
        if "2" in have and "3" in have:          # both edited: keep ours + copy of theirs
            other = git(folder, "show", f":3:{f}", binary=True)
            cname = conflict_name(f, "other device")
            (Path(folder) / cname).parent.mkdir(parents=True, exist_ok=True)
            (Path(folder) / cname).write_bytes(other)
            git(folder, "checkout", "--ours", "--", f)
            git(folder, "add", "--", f, cname)
            log(f"CONFLICT {f}  (other version saved as '{cname}')")
        elif "3" in have:                        # we deleted, they edited: keep their edit
            git(folder, "checkout", "--theirs", "--", f)
            git(folder, "add", "--", f)
            log(f"restored {f} (edited on other device)")
        else:                                    # they deleted, we edited: keep ours
            git(folder, "add", "--", f)
            log(f"kept {f} (edited here, deleted on other device)")
    git(folder, "commit", "-q", "--no-edit", "-m", f"{device}: merge (conflicts kept as copies)")


def adopt_remote(folder, remote):
    """New device: start from what's on GitHub, keep any local files on top."""
    git(folder, "reset", "-q", remote)
    missing = git(folder, "ls-files", "--deleted").splitlines()
    if missing:
        git(folder, "checkout", "-q", "--", *missing)


def pull(folder, branch, device):
    git(folder, "fetch", "-q", "origin")
    remote = f"origin/{branch}"
    if not git_ok(folder, "rev-parse", "--verify", "-q", remote):
        return False  # empty GitHub repo, nothing to pull yet
    if git(folder, "rev-list", "--count", f"HEAD..{remote}") == "0":
        return False
    if not git_ok(folder, "rev-parse", "--verify", "-q", "HEAD"):   # brand new local repo
        adopt_remote(folder, remote)
    elif not git_ok(folder, "merge", "-q", "--no-edit", remote):
        resolve_conflicts(folder, device)
    log("pulled changes from other device")
    return True


def push(folder, branch):
    if not git_ok(folder, "rev-parse", "--verify", "-q", "HEAD"):
        return
    remote = f"origin/{branch}"
    ahead = git(folder, "rev-list", "--count", f"{remote}..HEAD") \
        if git_ok(folder, "rev-parse", "--verify", "-q", remote) else "1"
    if ahead != "0":
        git(folder, "push", "-q", "-u", "origin", f"HEAD:{branch}")
        log("pushed to GitHub")


def sync_once(cfg, force=False):
    folder, branch, device = Path(cfg["folder"]), cfg["branch"], cfg["device_name"]
    status = git(folder, "status", "--porcelain")
    # Wait until you've stopped editing for a few seconds before committing.
    if status and (force or newest_change_age(folder, status) >= cfg["commit_after_seconds"]):
        commit_local(folder, device)
    pull(folder, branch, device)
    push(folder, branch)


def run_forever(cfg):
    log(f"gitsync started on '{cfg['device_name']}' for {cfg['folder']}")
    folder = Path(cfg["folder"])
    last_pull = 0
    while True:
        try:
            dirty = bool(git(folder, "status", "--porcelain"))
            due = time.time() - last_pull >= cfg["pull_every_seconds"]
            if dirty or due:
                sync_once(cfg)
                last_pull = time.time()
        except GitError as e:
            log(f"will retry: {e}")   # offline, laptop just woke up, etc.
        except Exception as e:
            log(f"error: {e!r}")
        time.sleep(3)


# ----------------------------------------------------------------- setup

def setup():
    print("=== gitsync setup ===\n")
    print("First make a PRIVATE, empty repository on github.com (New repository,")
    print("choose Private, don't add a README). Copy its HTTPS link.\n")
    url = input("1) GitHub repo link (https://github.com/you/name.git): ").strip()
    folder = Path(input("2) Folder on THIS computer to keep in sync: ").strip().strip('"'))
    name = input(f"3) Name for this device [{socket.gethostname()}]: ").strip() \
        or socket.gethostname()

    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / ".git").exists():
        git(folder, "init", "-q", "-b", "main")
        git(folder, "remote", "add", "origin", url)
    # Commits need a name; use the device name, only for this folder.
    git(folder, "config", "user.name", name)
    git(folder, "config", "user.email", f"{name}@gitsync.local")
    print("\nConnecting to GitHub (a sign-in window may pop up, log in there)...")
    try:
        git(folder, "fetch", "-q", "origin")
        if git_ok(folder, "rev-parse", "--verify", "-q", "origin/main") and \
                not git_ok(folder, "rev-parse", "--verify", "-q", "HEAD"):
            adopt_remote(folder, "origin/main")   # 2nd device: download everything
    except GitError as e:
        print(f"Could not reach GitHub: {e}")
    gi = folder / ".gitignore"
    if not gi.exists():
        gi.write_text(DEFAULT_GITIGNORE, encoding="utf-8")

    cfg = {"folder": str(folder), "branch": "main", "device_name": name,
           "commit_after_seconds": 5, "pull_every_seconds": 30}
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    try:
        sync_once(cfg, force=True)
        print("\nDone! Your folder is connected to GitHub.")
    except GitError as e:
        print(f"\nSync failed: {e}\nCheck the repo link and that you're signed in, "
              "then run:  python gitsync.py once")
    print("Start syncing:    python gitsync.py")
    print("Auto-start:       python gitsync.py install-startup")


def startup_script_path():
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/gitsync.vbs"


def install_startup():
    if os.name != "nt":
        print("Auto-start install is Windows only. On Mac/Linux add\n"
              f"  python3 '{Path(__file__).resolve()}'\nto your login items.")
        return
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.exists():
        pyw = Path(sys.executable)
    vbs = startup_script_path()
    vbs.write_text('CreateObject("WScript.Shell").Run """{}"" ""{}""", 0, False\r\n'
                   .format(pyw, Path(__file__).resolve()), encoding="utf-8")
    print(f"Installed: {vbs}\ngitsync now starts silently when you log in.")
    print(f"Log: {LOG_FILE}")


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
        sync_once(load_config(), force=True)
        log("done")
    elif cmd == "install-startup":
        install_startup()
    elif cmd == "remove-startup":
        remove_startup()
    elif cmd == "run":
        run_forever(load_config())
    else:
        print(__doc__)
