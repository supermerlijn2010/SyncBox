#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SyncBox server + website. Runs on the Raspberry Pi (or any always-on computer).

  * Website: browse, upload, download your synced files from any browser,
    restore deleted files, and download the SyncBox installer for a new PC.
  * API: the SyncBox program on your computers syncs through it.

Usage:
  python3 syncbox_web.py --set-password          # choose the website password
  python3 syncbox_web.py --root /srv/syncbox     # run (default port 80)
  python3 syncbox_web.py --root DIR --port 8080

Only the Python standard library is used (Python 3.9+).
"""

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF_FILE = Path(os.environ.get("SYNCBOX_WEB_CONF") or Path.home() / ".config" / "syncbox-web.json")
HUB_MARKER = ".syncbox_hub"
HUB_REGISTRY = "registry.json"
HUB_FOLDERS = "folders"
HUB_TRASH = ".trash"
PART_SUFFIX = ".syncbox-part"
FID_RE = re.compile(r"^[a-z0-9-]{1,60}$")

conf_lock = threading.Lock()
reg_lock = threading.Lock()
share_lock = threading.Lock()
SHARES_FILE = ".shares.json"
SESSIONS = {}   # cookie token -> expiry


# ============================================================ config / auth

def load_conf():
    try:
        return json.loads(CONF_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_conf(conf):
    CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF_FILE)


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def set_password(pw):
    with conf_lock:
        conf = load_conf()
        conf["salt"] = secrets.token_hex(16)
        conf["hash"] = hash_pw(pw, conf["salt"])
        save_conf(conf)


def check_password(pw):
    conf = load_conf()
    if "hash" not in conf:
        return False
    return hmac.compare_digest(hash_pw(pw or "", conf["salt"]), conf["hash"])


def new_device_token(name):
    token = secrets.token_hex(24)
    with conf_lock:
        conf = load_conf()
        conf.setdefault("devices", {})[uuid.uuid4().hex[:10]] = {
            "name": name, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "created": datetime.now().isoformat(timespec="seconds"), "last_seen": None}
        save_conf(conf)
    return token


def device_for_token(token):
    h = hashlib.sha256(token.encode()).hexdigest()
    for did, d in load_conf().get("devices", {}).items():
        if hmac.compare_digest(d["token_hash"], h):
            return did
    return None


_seen_cache = {}


def touch_device(did, name):
    now = time.time()
    if now - _seen_cache.get(did, 0) < 60:
        return
    _seen_cache[did] = now
    with conf_lock:
        conf = load_conf()
        d = conf.get("devices", {}).get(did)
        if d:
            d["last_seen"] = datetime.now().isoformat(timespec="seconds")
            if name:
                d["name"] = name[:60]
            save_conf(conf)


# ============================================================ storage

class BadRequest(Exception):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        (self.root / HUB_FOLDERS).mkdir(parents=True, exist_ok=True)
        (self.root / HUB_TRASH).mkdir(parents=True, exist_ok=True)
        if not (self.root / HUB_MARKER).exists():
            (self.root / HUB_MARKER).write_text("SyncBox server folder. Don't edit by hand.\n")
        if not (self.root / HUB_REGISTRY).exists():
            self.write_registry({})

    # ---- registry (list of shared folders)
    def registry(self):
        try:
            return json.loads((self.root / HUB_REGISTRY).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def write_registry(self, reg):
        tmp = self.root / f"{HUB_REGISTRY}.{uuid.uuid4().hex[:6]}.tmp"
        tmp.write_text(json.dumps(reg, indent=1), encoding="utf-8")
        os.replace(tmp, self.root / HUB_REGISTRY)

    def registry_op(self, op):
        with reg_lock:
            reg = self.registry()
            kind = op.get("op")
            fid = op.get("fid", "")
            if not FID_RE.match(fid):
                raise BadRequest("bad folder id")
            if kind == "set":
                e = op.get("entry") or {}
                if not isinstance(e, dict) or not str(e.get("name", "")).strip():
                    raise BadRequest("bad entry")
                reg[fid] = e
                (self.root / HUB_FOLDERS / fid).mkdir(parents=True, exist_ok=True)
            elif kind == "exclude":
                if fid in reg:
                    rel = str(op.get("rel", "")).strip("/")
                    ex = reg[fid].setdefault("exclude", [])
                    if rel and rel not in ex:
                        ex.append(rel)
            elif kind == "remove":
                if fid in reg:
                    del reg[fid]
                    src = self.root / HUB_FOLDERS / fid
                    if src.exists():
                        dst = self.root / HUB_TRASH / f"removed {datetime.now():%Y-%m-%d %H%M%S} {fid}"
                        shutil.move(str(src), str(dst))
            else:
                raise BadRequest("unknown op")
            self.write_registry(reg)
            return reg

    # ---- paths
    def folder(self, fid):
        if not FID_RE.match(fid or "") or fid not in self.registry():
            raise BadRequest("unknown folder")
        d = self.root / HUB_FOLDERS / fid
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def clean(rel):
        rel = (rel or "").replace("\\", "/").strip("/")
        if not rel:
            return ""
        parts = rel.split("/")
        for p in parts:
            if p in ("", ".", "..") or "\0" in p or p.endswith(PART_SUFFIX):
                raise BadRequest("bad path")
        return "/".join(parts)

    def path(self, fid, rel):
        return self.folder(fid) / self.clean(rel) if rel else self.folder(fid)

    # ---- file ops
    def scan(self, fid):
        base = self.folder(fid)
        out = {}
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, base).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir + "/"
            for n in filenames:
                if n.endswith(PART_SUFFIX):
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, n))
                except OSError:
                    continue
                out[rel_dir + n] = [st.st_size, st.st_mtime_ns]
        return out

    def listdir(self, fid, rel):
        p = self.path(fid, rel)
        if not p.is_dir():
            raise BadRequest("not a folder")
        items = []
        with os.scandir(p) as it:
            for e in it:
                if e.name.endswith(PART_SUFFIX):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                items.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size,
                              "mtime": int(st.st_mtime * 1000)})
        items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        return items

    def trash(self, fid, rel):
        src = self.path(fid, rel)
        if not rel or not src.exists():
            return
        dst = self.root / HUB_TRASH / fid / f"{datetime.now():%Y-%m-%d}" / self.clean(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst = dst.with_name(f"{dst.stem} {datetime.now():%H%M%S}{dst.suffix}")
        shutil.move(str(src), str(dst))
        base = self.folder(fid)
        parent = src.parent
        while parent != base and base in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def trash_list(self, limit=2000):
        reg = self.registry()
        out = []
        troot = self.root / HUB_TRASH
        for dirpath, _, filenames in os.walk(troot):
            for n in filenames:
                full = Path(dirpath) / n
                rel = full.relative_to(troot).as_posix()
                parts = rel.split("/")
                try:
                    st = full.stat()
                except OSError:
                    continue
                if parts[0].startswith("removed ") and len(parts) >= 2:
                    # a whole shared folder that was removed: "removed <date> <time> <fid>"
                    bits = parts[0].split(" ")
                    out.append({"path": rel, "folder": f"{bits[-1].rsplit('-', 1)[0]} (removed)",
                                "restorable": False, "file": "/".join(parts[1:]),
                                "deleted": bits[1] if len(bits) > 2 else "",
                                "size": st.st_size, "t": st.st_mtime})
                    continue
                fid = parts[0]
                out.append({"path": rel, "folder": reg.get(fid, {}).get("name", fid),
                            "restorable": fid in reg and len(parts) >= 3,
                            "file": "/".join(parts[2:]) if len(parts) >= 3 else rel,
                            "deleted": parts[1] if len(parts) >= 3 else "",
                            "size": st.st_size, "t": st.st_mtime})
        out.sort(key=lambda x: (x["deleted"], x["t"]), reverse=True)
        return out[:limit]

    def trash_restore(self, rel):
        rel = self.clean(rel)
        src = self.root / HUB_TRASH / rel
        parts = rel.split("/")
        if len(parts) < 3 or not src.is_file():
            raise BadRequest("can't restore this")
        dst = self.path(parts[0], "/".join(parts[2:]))
        if dst.exists():
            dst = dst.with_name(f"{dst.stem} (restored){dst.suffix}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        os.utime(dst)  # counts as a fresh change, so it syncs back to your computers

    def trash_empty(self):
        troot = self.root / HUB_TRASH
        shutil.rmtree(troot, ignore_errors=True)
        troot.mkdir(exist_ok=True)

    # ---- share links (anyone with the link can download, until it expires)
    def shares(self):
        try:
            s = json.loads((self.root / SHARES_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        now = time.time()
        return {k: v for k, v in s.items() if v.get("expires", 0) > now}

    def _save_shares(self, s):
        tmp = self.root / f"{SHARES_FILE}.{uuid.uuid4().hex[:6]}.tmp"
        tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
        os.replace(tmp, self.root / SHARES_FILE)

    def share_create(self, fid, rel, days):
        rel = self.clean(rel)
        p = self.path(fid, rel)
        if not rel or not p.exists():
            raise BadRequest("file not found")
        try:
            days = min(max(int(days or 7), 1), 90)
        except (TypeError, ValueError):
            raise BadRequest("bad number of days")
        token = secrets.token_urlsafe(18)
        with share_lock:
            s = self.shares()
            s[token] = {"f": fid, "p": rel, "name": p.name, "dir": p.is_dir(), "downloads": 0,
                        "created": datetime.now().isoformat(timespec="seconds"),
                        "expires": time.time() + days * 86400}
            self._save_shares(s)
        return token, s[token]

    def share_open(self, token):
        """(path, info) for a valid link, else None. Counts the download."""
        with share_lock:
            s = self.shares()
            v = s.get(token or "")
            if not v:
                return None
            try:
                p = self.path(v["f"], v["p"])
            except BadRequest:
                return None
            if not p.exists():
                return None
            v["downloads"] = v.get("downloads", 0) + 1
            self._save_shares(s)
            return p, v

    def share_revoke(self, token):
        with share_lock:
            s = self.shares()
            s.pop(token, None)
            self._save_shares(s)


# ============================================================ installer

INSTALL_BAT = r"""@echo off
title SyncBox installer
echo.
echo   Installing SyncBox...
echo.
set "D=%LOCALAPPDATA%\SyncBox"
if not exist "%D%" mkdir "%D%"
curl -fsS "{url}/download/syncbox.py" -o "%D%\syncbox.py"
if errorlevel 1 (
  echo Could not reach {url}
  echo Is this computer on the same network as your server?
  pause
  exit /b
)
> "%D%\server.txt" echo {url}
>> "%D%\server.txt" echo {token}

call :findpy
if not defined PY (
  echo Python is not installed yet. Installing it now, this can take a minute...
  winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
  call :findpy
)
if not defined PY (
  echo.
  echo Please install Python from the page that opens now
  echo ^(tick "Add python.exe to PATH"^), then run this installer again.
  start "" https://www.python.org/downloads/
  pause
  exit /b
)
"%PY%" "%D%\syncbox.py" --install
timeout /t 4 >nul
exit /b

:findpy
set "PY="
for %%P in ("%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" "%WINDIR%\py.exe") do if exist %%P if not defined PY set "PY=%%~P"
if not defined PY for /f "delims=" %%P in ('where py 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v WindowsApps') do if not defined PY set "PY=%%P"
exit /b
"""


def syncbox_client_source():
    for p in (HERE / "syncbox.py", HERE.parent / "client" / "syncbox.py"):
        if p.exists():
            return p.read_bytes()
    return None


# ============================================================ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "SyncBox"
    protocol_version = "HTTP/1.1"
    store = None  # set in main

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def send_json(self, data, code=200, headers=None):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, code, msg):
        self.close_connection = True   # request body may be unread; don't reuse connection
        self.send_json({"error": msg}, code, headers={"Connection": "close"})

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise BadRequest("too big")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise BadRequest("bad json")

    def cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sbsession":
                return v
        return None

    def auth(self):
        """'user' (browser session), 'device' (SyncBox app) or None."""
        t = self.cookie_token()
        if t and SESSIONS.get(t, 0) > time.time():
            SESSIONS[t] = time.time() + 30 * 86400
            return "user"
        a = self.headers.get("Authorization", "")
        if a.startswith("Bearer "):
            did = device_for_token(a[7:].strip())
            if did:
                touch_device(did, self.headers.get("X-Device", ""))
                return "device"
        return None

    def host_url(self):
        host = self.headers.get("Host") or "raspberrypi.local"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}"

    def q(self):
        u = urllib.parse.urlparse(self.path)
        return u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    # ---- routing
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method):
        path, q = self.q()
        try:
            # public
            if method == "GET" and path == "/":
                return self.send_bytes(INDEX_HTML.encode(), "text/html; charset=utf-8")
            if method == "GET" and path.startswith("/s/"):
                return self.serve_share(path[3:])
            if method == "GET" and path == "/download/syncbox.py":
                src = syncbox_client_source()
                if not src:
                    return self.send_error_json(404, "syncbox.py is missing next to syncbox_web.py")
                return self.send_bytes(src, "text/x-python; charset=utf-8")
            if method == "POST" and path == "/api/login":
                time.sleep(0.5)
                if not check_password(self.body_json().get("password")):
                    time.sleep(1)
                    return self.send_error_json(403, "Wrong password")
                t = secrets.token_hex(24)
                SESSIONS[t] = time.time() + 30 * 86400
                return self.send_json({"ok": True}, headers={
                    "Set-Cookie": f"sbsession={t}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"})
            if method == "POST" and path == "/api/device-token":
                b = self.body_json()
                time.sleep(0.5)
                if not check_password(b.get("password")):
                    time.sleep(1)
                    return self.send_error_json(403, "Wrong password")
                return self.send_json({"token": new_device_token(str(b.get("device") or "computer"))})

            who = self.auth()
            if method == "GET" and path == "/api/me":
                return self.send_json({"authed": who == "user", "has_password": "hash" in load_conf()})
            if not who:
                return self.send_error_json(401, "Please log in")
            if who == "user" and method != "GET" and \
                    not (self.headers.get("X-Requested-With") == "SyncBox"):
                return self.send_error_json(403, "missing header")  # CSRF guard
            return self.authed_route(method, path, q, who)
        except BadRequest as e:
            return self.send_error_json(400, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            sys.stderr.write(f"{datetime.now()} error {method} {self.path}: {e!r}\n")
            try:
                self.send_error_json(500, str(e))
            except Exception:
                pass

    def authed_route(self, method, path, q, who):
        st = self.store
        if method == "POST" and path == "/api/logout":
            SESSIONS.pop(self.cookie_token(), None)
            return self.send_json({"ok": True})
        if method == "GET" and path == "/api/registry":
            return self.send_json(st.registry())
        if method == "POST" and path == "/api/registry":
            return self.send_json(st.registry_op(self.body_json()))
        if method == "GET" and path == "/api/scan":
            return self.send_json(st.scan(q.get("f")))
        if method == "GET" and path == "/api/list":
            return self.send_json(st.listdir(q.get("f"), q.get("p", "")))
        if method == "GET" and path == "/api/hash":
            h = hashlib.sha256()
            with open(st.path(q.get("f"), q.get("p")), "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return self.send_json({"sha256": h.hexdigest()})
        if method == "GET" and path == "/api/file":
            p = st.path(q.get("f"), q.get("p", ""))
            if p.is_dir():
                return self.send_zip(p, q.get("name") or p.name or "files")
            return self.send_file(p, download=q.get("dl") == "1")
        if method == "PUT" and path == "/api/file":
            return self.receive_file(q)
        if method == "POST" and path == "/api/delete":
            b = self.body_json()
            st.trash(b.get("f"), st.clean(b.get("p")))
            return self.send_json({"ok": True})
        if method == "POST" and path == "/api/mkdir":
            b = self.body_json()
            st.path(b.get("f"), st.clean(b.get("p"))).mkdir(parents=True, exist_ok=True)
            return self.send_json({"ok": True})
        if method == "POST" and path == "/api/share":
            b = self.body_json()
            token, v = st.share_create(b.get("f"), b.get("p"), b.get("days"))
            return self.send_json({"url": f"{self.host_url()}/s/{token}", "expires": v["expires"]})
        if method == "GET" and path == "/api/shares":
            reg = st.registry()
            return self.send_json(sorted(
                [{"token": k, "url": f"{self.host_url()}/s/{k}", "name": v["name"], "path": v["p"],
                  "folder": reg.get(v["f"], {}).get("name", v["f"]), "dir": v.get("dir", False),
                  "expires": v["expires"], "downloads": v.get("downloads", 0)}
                 for k, v in st.shares().items()], key=lambda x: -x["expires"]))
        if method == "POST" and path == "/api/shares/revoke":
            st.share_revoke(self.body_json().get("token"))
            return self.send_json({"ok": True})
        if method == "GET" and path == "/api/trash":
            return self.send_json(st.trash_list())
        if method == "POST" and path == "/api/trash/restore":
            st.trash_restore(self.body_json().get("path"))
            return self.send_json({"ok": True})
        if method == "POST" and path == "/api/trash/empty" and who == "user":
            st.trash_empty()
            return self.send_json({"ok": True})
        if method == "GET" and path == "/api/devices" and who == "user":
            devs = load_conf().get("devices", {})
            return self.send_json([{"id": k, "name": v["name"], "created": v["created"],
                                    "last_seen": v["last_seen"]} for k, v in devs.items()])
        if method == "POST" and path == "/api/devices/revoke" and who == "user":
            did = self.body_json().get("id")
            with conf_lock:
                conf = load_conf()
                conf.get("devices", {}).pop(did, None)
                save_conf(conf)
            return self.send_json({"ok": True})
        if method == "GET" and path == "/download/install.bat" and who == "user":
            token = new_device_token("new computer (not connected yet)")
            bat = INSTALL_BAT.replace("{url}", self.host_url()).replace("{token}", token)
            return self.send_bytes(bat.replace("\n", "\r\n").encode(), "application/octet-stream",
                                   filename="Install SyncBox.bat")
        return self.send_error_json(404, "not found")

    # ---- share links
    def share_store(self, token):
        """Which storage a share link belongs to (SyncBox Cloud has one per customer)."""
        return self.store

    def serve_share(self, token):
        st = self.share_store(token)
        found = st.share_open(token) if st else None
        if not found:
            page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                    "<title>Link expired</title><body style='font:16px system-ui;text-align:center;"
                    "padding:15vh 20px;color:#444'><h1>This link doesn't work any more</h1>"
                    "<p>It has expired or was removed by the person who shared it.</p>").encode()
            self.close_connection = True
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(page)
            return
        p, info = found
        if p.is_dir():
            return self.send_zip(p, info["name"])
        return self.send_file(p, download=True)

    # ---- file transfer
    def send_bytes(self, data, ctype, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, p, download):
        if not p.is_file():
            return self.send_error_json(404, "not found")
        st = p.stat()
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        # Uploaded files are untrusted: an opened .html/.svg must not run scripts on this site.
        self.send_header("Content-Security-Policy", "sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Mtime-Ns", str(st.st_mtime_ns))
        disp = "attachment" if download else "inline"
        self.send_header("Content-Disposition",
                         f"{disp}; filename*=UTF-8''{urllib.parse.quote(p.name)}")
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def send_zip(self, folder, name):
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}.zip")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        class Out:  # write-only stream for ZipFile
            def __init__(s, w):
                s.w, s.n = w, 0

            def write(s, b):
                s.w.write(b)
                s.n += len(b)
                return len(b)

            def flush(s):
                s.w.flush()

        with zipfile.ZipFile(Out(self.wfile), "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for dirpath, _, files in os.walk(folder):
                for fn in files:
                    if fn.endswith(PART_SUFFIX):
                        continue
                    full = Path(dirpath) / fn
                    try:
                        z.write(full, full.relative_to(folder).as_posix())
                    except OSError:
                        pass

    def receive_file(self, q):
        st = self.store
        rel = st.clean(q.get("p"))
        if not rel:
            raise BadRequest("missing path")
        dst = st.path(q.get("f"), rel)
        n = int(self.headers.get("Content-Length") or -1)
        if n < 0:
            raise BadRequest("Content-Length required")
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f"{dst.name}.{uuid.uuid4().hex[:6]}{PART_SUFFIX}")
        try:
            with open(tmp, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise BadRequest("upload interrupted")
                    f.write(chunk)
                    left -= len(chunk)
            mt = q.get("mtime_ns")
            if mt and mt.isdigit():
                os.utime(tmp, ns=(int(mt), int(mt)))
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()
        s = dst.stat()
        return self.send_json({"sig": [s.st_size, s.st_mtime_ns]})


# ============================================================ website (single page)

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SyncBox</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#1b1f24;--muted:#667085;--line:#e4e7ec;--accent:#2563eb;
--accent-soft:#e8efff;--danger:#d92d20;--ok:#079455;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--text:#e8eaed;--muted:#98a2b3;
--line:#2a2f37;--accent:#6b9bff;--accent-soft:#1c2740;--danger:#f97066;--ok:#47cd89;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}
header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.bar{max-width:1080px;margin:auto;display:flex;align-items:center;gap:16px;padding:12px 16px}
.logo{font-weight:700;font-size:19px;display:flex;align-items:center;gap:8px;color:var(--text)}
.logo svg{color:var(--accent)}
nav{display:flex;gap:4px;margin-left:12px}
nav a{padding:6px 12px;border-radius:8px;color:var(--muted);font-weight:500}
nav a.on{background:var(--accent-soft);color:var(--accent)}
.sp{flex:1}
main{max-width:1080px;margin:auto;padding:24px 16px 120px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:20px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:0 0 12px}
.muted{color:var(--muted)}
.btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--card);color:var(--text);
padding:8px 14px;border-radius:9px;font:inherit;font-weight:500;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.danger{color:var(--danger)}
.btn.small{padding:4px 10px;font-size:13px}
.hero{display:grid;grid-template-columns:1.2fr 1fr;gap:20px;margin-bottom:24px}
.steps{margin:12px 0 0;padding-left:20px}.steps li{margin:4px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.folder{cursor:pointer;display:flex;gap:12px;align-items:flex-start}
.folder:hover{border-color:var(--accent)}
.ficon{flex:none;width:40px;height:40px;border-radius:10px;background:var(--accent-soft);color:var(--accent);display:grid;place-items:center}
.fname{font-weight:600;word-break:break-word}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0 16px}
.crumbs{font-size:15px;display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.crumbs span{color:var(--muted)}
table{width:100%;border-collapse:collapse}
td,th{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left}
th{font-size:13px;color:var(--muted);font-weight:500}
tr:last-child td{border-bottom:none}
td.n{width:110px;color:var(--muted);font-size:14px}
td.a{width:1%;white-space:nowrap;text-align:right}
.row-name{display:flex;align-items:center;gap:10px;word-break:break-all}
.empty{padding:40px;text-align:center;color:var(--muted)}
#drop{position:fixed;inset:0;background:rgba(37,99,235,.12);border:3px dashed var(--accent);display:none;
place-items:center;font-size:22px;font-weight:600;color:var(--accent);z-index:20}
#prog{position:fixed;left:16px;right:16px;bottom:16px;max-width:560px;margin:auto;display:none;z-index:30}
.pbar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:8px}
.pbar div{height:100%;background:var(--accent);width:0}
.login{max-width:360px;margin:12vh auto}
input[type=password],input[type=text]{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;
background:var(--bg);color:var(--text);font:inherit;margin:8px 0 12px}
.err{color:var(--danger);min-height:22px}
.tag{font-size:12px;padding:2px 8px;border-radius:99px;background:var(--accent-soft);color:var(--accent)}
.toast{position:fixed;top:70px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:10px 16px;border-radius:10px;z-index:40}
@media (max-width:720px){.hero{grid-template-columns:1fr}td.n.hide{display:none}nav{margin-left:0}.logo span{display:none}}
</style></head><body>
<div id="app"></div>
<div id="drop">Drop files or folders to upload</div>
<div id="prog" class="card"><div id="ptext">Uploading...</div><div class="pbar"><div id="pfill"></div></div></div>
<input type="file" id="pickFiles" multiple hidden>
<input type="file" id="pickDir" webkitdirectory hidden>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ICON = {
 folder:'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
 file:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
 sync:'<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M20 11a8 8 0 0 0-14.5-4.5L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.5 4.5L20 16"/><path d="M20 20v-4h-4"/></svg>',
 drive:'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M5 13l2-8h10l2 8"/><circle cx="17" cy="16.5" r="1"/></svg>',
 home:'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/></svg>'};
async function api(path, opts={}) {
  opts.headers = Object.assign({'X-Requested-With':'SyncBox'}, opts.headers||{});
  if (opts.json !== undefined) { opts.method = opts.method||'POST'; opts.body = JSON.stringify(opts.json); opts.headers['Content-Type']='application/json'; }
  const r = await fetch(path, opts);
  if (r.status === 401) { render(); throw new Error('login'); }
  const d = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(d.error || ('Error ' + r.status));
  return d;
}
function toast(msg){const t=document.createElement('div');t.className='toast';t.textContent=msg;document.body.append(t);setTimeout(()=>t.remove(),2600)}
const size = n => n<1024? n+' B' : n<1048576? (n/1024).toFixed(0)+' KB' : n<1073741824? (n/1048576).toFixed(1)+' MB' : (n/1073741824).toFixed(2)+' GB';
const when = ms => { const d=new Date(ms); return d.toLocaleDateString()+' '+d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); };
const enc = encodeURIComponent;
let REG = {};
function route(){ const h=location.hash.slice(1)||'/'; const p=h.split('/').filter(Boolean).map(decodeURIComponent); return p; }
function shell(active, inner){
  return `<header><div class="bar"><a class="logo" href="#/">${ICON.sync}<span>SyncBox</span></a>
  <nav><a href="#/" class="${active==='files'?'on':''}">Files</a><a href="#/trash" class="${active==='trash'?'on':''}">Trash</a><a href="#/links" class="${active==='links'?'on':''}">Links</a><a href="#/devices" class="${active==='devices'?'on':''}">Computers</a></nav>
  <div class="sp"></div><button class="btn small" onclick="logout()">Log out</button></div></header><main>${inner}</main>`;
}
async function render(){
  const me = await fetch('/api/me').then(r=>r.json());
  if (!me.authed) return loginView(me);
  const p = route();
  try {
    if (p[0]==='f') return await browseView(p[1], p.slice(2).join('/'));
    if (p[0]==='trash') return await trashView();
    if (p[0]==='devices') return await devicesView();
    if (p[0]==='links') return await linksView();
    return await homeView();
  } catch(e){ if (e.message!=='login') { $('#app').innerHTML = shell('', `<div class="card empty">${esc(e.message)}<br><br><a href="#/">Back</a></div>`);} }
}
function loginView(me){
  $('#app').innerHTML = `<div class="login card"><div class="logo" style="margin-bottom:12px">${ICON.sync} SyncBox</div>
  ${me.has_password ? `<form onsubmit="login(event)"><label>Password</label><input type="password" id="pw" autofocus>
  <div class="err" id="err"></div><button class="btn primary" style="width:100%;justify-content:center">Log in</button></form>`
  : `<p>No password set yet. On the server run:</p><pre>python3 syncbox_web.py --set-password</pre>`}</div>`;
}
async function login(e){ e.preventDefault(); $('#err').textContent='';
  try { await api('/api/login', {json:{password:$('#pw').value}}); render(); } catch(err){ $('#err').textContent=err.message; } }
async function logout(){ await api('/api/logout',{json:{}}).catch(()=>{}); render(); }
function fIcon(e){ return e.drive? ICON.drive : (e.home_rel===''? ICON.home : ICON.folder); }
async function homeView(){
  REG = await api('/api/registry');
  const ids = Object.keys(REG).sort((a,b)=>REG[a].name.localeCompare(REG[b].name));
  $('#app').innerHTML = shell('files', `
  <div class="hero">
    <div class="card"><h1>Add a computer</h1>
      <p class="muted" style="margin:0">Install SyncBox on a Windows PC. Its folders then sync automatically in the background.</p>
      <ol class="steps"><li>Click the button on the computer you want to add</li><li>Open the downloaded file (if Windows warns: <b>More info</b> &rarr; <b>Run anyway</b>)</li>
      <li>Choose folders or a whole drive in the SyncBox window</li></ol>
      <a class="btn primary" style="margin-top:12px" href="/download/install.bat">&#x2B07; Download SyncBox for Windows</a>
    </div>
    <div class="card"><h2>Share from this browser</h2>
      <p class="muted" style="margin-top:0">Pick a folder to upload. Computers with SyncBox will get it automatically, in <i>SyncBox</i> in their user folder.</p>
      <button class="btn" onclick="shareFolder()">&#x1F4C1; Choose a folder...</button>
      <button class="btn" onclick="newShared()">+ New empty folder</button>
    </div>
  </div>
  <h2>Shared folders <span class="muted" style="font-weight:400">(${ids.length})</span></h2>
  ${ids.length? `<div class="grid">${ids.map(id=>{const e=REG[id]; return `<div class="card folder" onclick="location.hash='#/f/${enc(id)}'">
    <div class="ficon">${fIcon(e)}</div><div><div class="fname">${esc(e.name)}</div>
    <div class="muted" style="font-size:13px">from ${esc(e.added_by||'?')}${e.exclude&&e.exclude.length? ' &middot; '+e.exclude.length+' excluded':''}</div></div></div>`}).join('')}</div>`
  : `<div class="card empty">Nothing shared yet. Install SyncBox on a computer and pick folders, or upload one here.</div>`}`);
}
let CUR = {f:null, p:''};
async function browseView(fid, path){
  if (!REG[fid]) REG = await api('/api/registry');
  const e = REG[fid]; if (!e) throw new Error('This folder is no longer shared.');
  CUR = {f:fid, p:path};
  const items = await api(`/api/list?f=${enc(fid)}&p=${enc(path)}`);
  const parts = path? path.split('/') : [];
  let crumbs = `<a href="#/">Files</a><span>/</span><a href="#/f/${enc(fid)}">${esc(e.name)}</a>`;
  parts.forEach((x,i)=>{ crumbs += `<span>/</span><a href="#/f/${enc(fid)}/${parts.slice(0,i+1).map(enc).join('/')}">${esc(x)}</a>`; });
  const sub = p => (path? path+'/' : '') + p;
  $('#app').innerHTML = shell('files', `<div class="crumbs">${crumbs}</div>
  <div class="toolbar">
    <button class="btn primary" onclick="$('#pickFiles').click()">&#x2B06; Upload files</button>
    <button class="btn" onclick="$('#pickDir').click()">&#x2B06; Upload folder</button>
    <button class="btn" onclick="mkdir()">+ New folder</button>
    <a class="btn" href="/api/file?f=${enc(fid)}&p=${enc(path)}&name=${enc(parts.length?parts[parts.length-1]:e.name)}">&#x2B07; Download all (.zip)</a>
    <div class="sp"></div>${path? '' : `<button class="btn danger small" onclick="unshare()">Stop sharing</button>`}
  </div>
  <div class="card" style="padding:4px 10px">${items.length? `<table><tr><th>Name</th><th class="hide">Size</th><th class="hide">Modified</th><th></th></tr>
  ${items.map(it=>`<tr><td><div class="row-name">${it.dir?ICON.folder:ICON.file}
   ${it.dir? `<a href="#/f/${enc(fid)}/${sub(it.name).split('/').map(enc).join('/')}">${esc(it.name)}</a>`
           : `<a href="/api/file?f=${enc(fid)}&p=${enc(sub(it.name))}" target="_blank">${esc(it.name)}</a>`}</div></td>
   <td class="n hide">${it.dir?'':size(it.size)}</td><td class="n hide">${when(it.mtime)}</td>
   <td class="a"><a class="btn small" title="Download" href="/api/file?f=${enc(fid)}&p=${enc(sub(it.name))}&dl=1${it.dir?'&name='+enc(it.name):''}">&#x2B07;</a>
   <button class="btn small" title="Get a share link" onclick='share(${JSON.stringify(sub(it.name))})'>&#x1F517;</button>
   <button class="btn small danger" title="Delete" onclick='del(${JSON.stringify(sub(it.name))})'>&#x1F5D1;</button></td></tr>`).join('')}</table>`
  : `<div class="empty">Empty folder. Drag files here to upload.</div>`}</div>`);
}
async function mkdir(){ const n = prompt('Folder name:'); if(!n) return;
  if (/[\\/]/.test(n)) return alert('No slashes please.');
  await api('/api/mkdir',{json:{f:CUR.f,p:(CUR.p?CUR.p+'/':'')+n}}).catch(e=>alert(e.message)); render(); }
async function del(p){ if(!confirm(`Delete "${p.split('/').pop()}"?\n\nIt is also removed on your computers (you can get it back from Trash).`)) return;
  await api('/api/delete',{json:{f:CUR.f,p}}).catch(e=>alert(e.message)); render(); }
async function unshare(){ if(!confirm('Stop sharing this folder on ALL computers?\n\nFiles on your computers stay where they are. The copy on the server goes to Trash.')) return;
  await api('/api/registry',{json:{op:'remove',fid:CUR.f}}); location.hash='#/'; }
function newId(name){ return (name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'').slice(0,30)||'folder')+'-'+Math.random().toString(16).slice(2,8); }
async function createShared(name){
  const fid = newId(name);
  await api('/api/registry',{json:{op:'set',fid,entry:{name,home_rel:null,exclude:[],added_by:'website',added_at:new Date().toISOString().slice(0,19)}}});
  REG = await api('/api/registry'); return fid;
}
async function newShared(){ const n=prompt('Name of the new shared folder:'); if(!n) return; const fid=await createShared(n.trim()); location.hash='#/f/'+enc(fid); }
let SHARE_NEXT = false;
function shareFolder(){ SHARE_NEXT = true; $('#pickDir').click(); }
// ---------- uploads
async function uploadList(list){ // [{file, rel}]
  if (!list.length) return;
  const prog=$('#prog'); prog.style.display='block'; let done=0, bytes=0, total=list.reduce((a,x)=>a+x.file.size,0), failed=[];
  const next = async () => { while (list.length){ const {file,rel,fid}=list.shift();
      $('#ptext').textContent = `Uploading ${done+1} of ${done+list.length+1}: ${rel}`;
      try { const r = await fetch(`/api/file?f=${enc(fid)}&p=${enc(rel)}&mtime_ns=${file.lastModified}000000`,
              {method:'PUT', body:file, headers:{'X-Requested-With':'SyncBox'}});
            if(!r.ok) throw new Error((await r.json().catch(()=>({}))).error||r.status); }
      catch(e){ failed.push(rel+': '+e.message); }
      done++; bytes+=file.size; $('#pfill').style.width = (total? bytes/total*100 : 100)+'%'; } };
  await Promise.all([next(),next(),next()]);
  prog.style.display='none'; $('#pfill').style.width='0';
  if (failed.length) alert('Some files failed:\n'+failed.slice(0,10).join('\n')); else toast(`Uploaded ${done} file(s)`);
  render();
}
const SKIP = /^(~\$|\.~lock\.)|\.(tmp|swp)$|^(desktop\.ini|thumbs\.db|\.ds_store)$/i;
$('#pickFiles').onchange = e => { const fs=[...e.target.files]; e.target.value='';
  uploadList(fs.map(f=>({file:f, fid:CUR.f, rel:(CUR.p?CUR.p+'/':'')+f.name}))); };
$('#pickDir').onchange = async e => { const fs=[...e.target.files].filter(f=>!SKIP.test(f.name)); e.target.value='';
  if (!fs.length) return;
  if (SHARE_NEXT){ SHARE_NEXT=false; const top=fs[0].webkitRelativePath.split('/')[0]; const fid=await createShared(top);
    location.hash='#/f/'+enc(fid); CUR={f:fid,p:''};
    return uploadList(fs.map(f=>({file:f, fid, rel:f.webkitRelativePath.split('/').slice(1).join('/')}))); }
  uploadList(fs.map(f=>({file:f, fid:CUR.f, rel:(CUR.p?CUR.p+'/':'')+f.webkitRelativePath}))); };
// drag & drop (files and folders)
let dragN=0;
addEventListener('dragenter',e=>{ if(route()[0]!=='f')return; e.preventDefault(); dragN++; $('#drop').style.display='grid'; });
addEventListener('dragleave',e=>{ if(--dragN<=0){dragN=0;$('#drop').style.display='none';} });
addEventListener('dragover',e=>{ if(route()[0]==='f') e.preventDefault(); });
addEventListener('drop',async e=>{ if(route()[0]!=='f')return; e.preventDefault(); dragN=0; $('#drop').style.display='none';
  const out=[]; const base=CUR.p?CUR.p+'/':''; const fid=CUR.f;
  const walk = (entry, path) => new Promise(res => {
    if (entry.isFile) entry.file(f=>{ if(!SKIP.test(f.name)) out.push({file:f,fid,rel:base+path+f.name}); res(); }, ()=>res());
    else { const r=entry.createReader(); const all=[]; const read=()=>r.readEntries(async es=>{ if(!es.length){ for(const x of all) await walk(x, path+entry.name+'/'); res(); } else { all.push(...es); read(); } }, ()=>res()); read(); } });
  const entries=[...e.dataTransfer.items].map(i=>i.webkitGetAsEntry&&i.webkitGetAsEntry()).filter(Boolean);
  for (const en of entries) await walk(en, '');
  uploadList(out); });
// ---------- trash / devices
async function copyText(text, okMsg){
  try { await navigator.clipboard.writeText(text); toast(okMsg); }
  catch(e){ prompt('Copy this link:', text); }
}
async function share(p){
  const days = prompt(`Share "${p.split('/').pop()}" with a link.\n\nAnyone with the link can download it. How many days should it work? (1-90)`, '7');
  if (!days) return;
  try { const d = await api('/api/share',{json:{f:CUR.f,p,days:Number(days)}});
        await copyText(d.url, `Link copied! It works for ${days} day(s).`); }
  catch(e){ alert(e.message); }
}
async function linksView(){
  const ls = await api('/api/shares');
  $('#app').innerHTML = shell('links', `<h1>Share links</h1><p class="muted">Links you made with the &#x1F517; button. Anyone with a link can download that file or folder until it expires.</p>
  <div class="card" style="padding:4px 10px">${ls.length? `<table><tr><th>File</th><th class="hide">Folder</th><th class="hide">Works until</th><th class="hide">Downloads</th><th></th></tr>
  ${ls.map(l=>`<tr><td><div class="row-name">${l.dir?ICON.folder:ICON.file}${esc(l.name)}</div></td><td class="n hide">${esc(l.folder)}</td>
  <td class="n hide">${new Date(l.expires*1000).toLocaleDateString()}</td><td class="n hide">${l.downloads}</td>
  <td class="a"><button class="btn small" onclick='copyText(${JSON.stringify(l.url)}, "Link copied!")'>Copy link</button>
  <button class="btn small danger" onclick='unshareLink(${JSON.stringify(l.token)})'>Remove</button></td></tr>`).join('')}</table>`
  : '<div class="empty">No share links yet. Open a folder and click &#x1F517; next to a file.</div>'}</div>`);
}
async function unshareLink(t){ if(!confirm('Remove this link? People who have it can no longer download.')) return;
  await api('/api/shares/revoke',{json:{token:t}}); render(); }
async function trashView(){
  const items = await api('/api/trash');
  $('#app').innerHTML = shell('trash', `<div class="toolbar"><div><h1>Trash</h1><div class="muted">Files deleted on any computer end up here.</div></div><div class="sp"></div>
  ${items.length?'<button class="btn danger" onclick="emptyTrash()">Empty trash</button>':''}</div>
  <div class="card" style="padding:4px 10px">${items.length? `<table><tr><th>File</th><th class="hide">Folder</th><th class="hide">Deleted</th><th class="hide">Size</th><th></th></tr>
  ${items.map(it=>`<tr><td><div class="row-name">${ICON.file}${esc(it.file)}</div></td><td class="n hide">${esc(it.folder)}</td><td class="n hide">${esc(it.deleted)}</td>
  <td class="n hide">${size(it.size)}</td><td class="a">${it.restorable? `<button class="btn small" onclick='restore(${JSON.stringify(it.path)})'>Restore</button>`:''}</td></tr>`).join('')}</table>`
  : '<div class="empty">Trash is empty.</div>'}</div>`);
}
async function restore(p){ await api('/api/trash/restore',{json:{path:p}}).catch(e=>alert(e.message)); toast('Restored, it will sync back to your computers'); render(); }
async function emptyTrash(){ if(!confirm('Permanently delete everything in the trash? This cannot be undone.')) return; await api('/api/trash/empty',{json:{}}); render(); }
async function devicesView(){
  const ds = await api('/api/devices');
  $('#app').innerHTML = shell('devices', `<h1>Computers</h1><p class="muted">Computers that can sync with this server. Remove one if it's lost or you no longer use it.</p>
  <div class="card" style="padding:4px 10px">${ds.length? `<table><tr><th>Name</th><th class="hide">Added</th><th class="hide">Last seen</th><th></th></tr>
  ${ds.map(d=>`<tr><td>${esc(d.name)}</td><td class="n hide">${esc((d.created||'').replace('T',' ').slice(0,16))}</td>
  <td class="n hide">${d.last_seen? esc(d.last_seen.replace('T',' ').slice(0,16)) : '<span class="tag">never</span>'}</td>
  <td class="a"><button class="btn small danger" onclick='revoke(${JSON.stringify(d.id)})'>Remove</button></td></tr>`).join('')}</table>`
  : '<div class="empty">No computers yet. Use "Download SyncBox for Windows" on the Files page.</div>'}</div>`);
}
async function revoke(id){ if(!confirm('Remove this computer? It stops syncing until you install SyncBox on it again.')) return; await api('/api/devices/revoke',{json:{id}}); render(); }
addEventListener('hashchange', render); render();
</script></body></html>
"""


# ============================================================ main

def main():
    ap = argparse.ArgumentParser(description="SyncBox server + website")
    ap.add_argument("--root", default=str(Path.home() / "syncbox-data"), help="where files are stored")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--set-password", action="store_true")
    a = ap.parse_args()

    if a.set_password:
        import getpass
        pw = getpass.getpass("New website password: ")
        if len(pw) < 6 or pw != getpass.getpass("Again: "):
            print("Passwords don't match or are shorter than 6 characters.")
            sys.exit(1)
        set_password(pw)
        print("Password saved.")
        return

    Handler.store = Store(a.root)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"SyncBox server running on port {a.port}, files in {Handler.store.root}")
    if "hash" not in load_conf():
        print("WARNING: no password yet. Run: python3 syncbox_web.py --set-password")
    srv.serve_forever()


if __name__ == "__main__":
    main()
