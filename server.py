#!/usr/bin/env python3
"""Storage server for the Export Quote Calculator (Python standard library only).

Serves index.html and keeps every record in a folder you choose:

    <data folder>/
        items/<id>.json      quote-list entries
        quotes/<id>.json     generated-quotation records (search metadata)
        files/<name>.xlsx    the generated Excel / PDF files themselves
        users.json           accounts (server mode only; passwords are salted PBKDF2 hashes)
        backups/             daily zip backups (server mode only)

Personal use (one PC):
    python server.py                         # http://127.0.0.1:8765/, no login
    python server.py --data-dir D:\\Quotes    # choose the folder (remembered in config.json)

Shared use (company server, several people):
    python server.py --host 0.0.0.0 --port 8765 --data-dir D:\\QuoteData
    -> login is required automatically. On first start open http://127.0.0.1:8765/ ON THE SERVER
       to create the administrator account; the admin adds the other users in the web page.
    Optional HTTPS: add  --cert fullchain.pem --key privkey.pem

Account maintenance from the command line (e.g. lost admin password):
    python server.py user list|add NAME [--admin]|passwd NAME|del NAME  [--data-dir ...]
"""
import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

FROZEN = getattr(sys, "frozen", False)          # True when packaged as QuoteCalculator.exe
# Bundled resources (index.html) live in PyInstaller's temp folder; config and the default
# data folder live next to the .exe (or next to this script) so they survive restarts.
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
DEFAULT_DIR = ROOT / "报价数据" if FROZEN else ROOT / "quote_data"
MAX_BODY = 64 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,149}\.(xlsx|pdf)$")
USER_RE = re.compile(r"^[\w.-]{2,32}$")
MIME = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
}
KINDS = ("items", "quotes")
COOKIE = "qc_session"
SESSION_SECS = 12 * 3600
PBKDF2_ITERS = 200_000
MAX_FAILS, LOCK_SECS = 5, 600
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class Store:
    """All disk access. A lock serialises writes; files are replaced atomically."""

    def __init__(self, data_dir):
        self.lock = threading.Lock()
        self.set_dir(data_dir)

    def set_dir(self, data_dir):
        path = Path(data_dir).expanduser()
        if not path.is_absolute():
            raise ValueError("path must be absolute")
        for sub in ("items", "quotes", "files"):
            (path / sub).mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        self.dir = path.resolve()

    def _write(self, target, data):
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def read_json(self, kind, rid):
        try:
            return json.loads((self.dir / kind / (rid + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list(self, kind):
        out = []
        for f in sorted((self.dir / kind).glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue  # skip unreadable / half-edited files
        return out

    def put_json(self, kind, rid, obj, user=None):
        with self.lock:
            if user:  # server mode: the server, not the browser, decides who created a record
                prev = self.read_json(kind, rid) or {}
                obj["createdBy"] = prev.get("createdBy") or user
                obj["updatedBy"] = user
            self._write(self.dir / kind / (rid + ".json"), json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def delete(self, kind, rid):
        with self.lock:
            meta = self.dir / kind / (rid + ".json")
            if kind == "quotes" and meta.exists():
                try:
                    for f in json.loads(meta.read_text(encoding="utf-8")).get("files", []):
                        name = f.get("name", "")
                        if FILE_RE.match(name):
                            (self.dir / "files" / name).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
            meta.unlink(missing_ok=True)

    def put_file(self, name, data):
        with self.lock:
            self._write(self.dir / "files" / name, data)

    def get_file(self, name):
        path = self.dir / "files" / name
        return path.read_bytes() if path.is_file() else None

    def backup(self, keep=14):
        """Zip items/quotes/files/users into backups/backup-YYYYMMDD.zip (once a day)."""
        with self.lock:
            bdir = self.dir / "backups"
            bdir.mkdir(exist_ok=True)
            target = bdir / ("backup-%s.zip" % datetime.now().strftime("%Y%m%d"))
            if target.exists():
                return
            tmp = target.with_suffix(".tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for sub in ("items", "quotes", "files"):
                    for f in (self.dir / sub).glob("*"):
                        if f.is_file() and not f.name.startswith(".tmp-"):
                            z.write(f, "%s/%s" % (sub, f.name))
                if (self.dir / "users.json").exists():
                    z.write(self.dir / "users.json", "users.json")
            os.replace(tmp, target)
            for old in sorted(bdir.glob("backup-*.zip"))[:-keep]:
                old.unlink(missing_ok=True)


class Auth:
    """Accounts (users.json), signed session cookies and login throttling."""

    def __init__(self, store):
        self.store = store
        self.fails = {}
        self.lock = threading.Lock()

    # ---- accounts ----
    def _file(self):
        return self.store.dir / "users.json"

    def users(self):
        try:
            return json.loads(self._file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, users):
        self.store._write(self._file(), json.dumps(users, ensure_ascii=False, indent=2).encode("utf-8"))

    @staticmethod
    def _hash(pw, salt, iters=PBKDF2_ITERS):
        return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iters).hex()

    def set_user(self, name, pw, admin=None):
        if not USER_RE.match(name):
            raise ValueError("用户名需 2-32 位字母、数字、汉字或 . _ -  /  username: 2-32 letters, digits, . _ -")
        if len(pw) < 8:
            raise ValueError("密码至少 8 位 / password needs at least 8 characters")
        with self.lock:
            users = self.users()
            salt = secrets.token_bytes(16)
            old = users.get(name, {})
            users[name] = {"salt": salt.hex(), "hash": self._hash(pw, salt), "iters": PBKDF2_ITERS,
                           "admin": bool(old.get("admin")) if admin is None else bool(admin)}
            self._save(users)

    def delete_user(self, name):
        with self.lock:
            users = self.users()
            if name not in users:
                raise ValueError("no such user")
            if users[name].get("admin") and sum(1 for u in users.values() if u.get("admin")) < 2:
                raise ValueError("不能删除最后一个管理员 / cannot delete the last administrator")
            del users[name]
            self._save(users)

    def is_admin(self, name):
        return bool(self.users().get(name, {}).get("admin"))

    def verify(self, name, pw, ip):
        key = (ip, name.lower())
        now = time.time()
        with self.lock:
            cnt, first = self.fails.get(key, (0, now))
            if cnt >= MAX_FAILS and now - first < LOCK_SECS:
                return "locked"
            if now - first >= LOCK_SECS:
                cnt, first = 0, now
        u = self.users().get(name)
        salt = bytes.fromhex(u["salt"]) if u else b"\0" * 16          # same work for unknown users
        ok = hmac.compare_digest(self._hash(pw, salt, u["iters"] if u else PBKDF2_ITERS), u["hash"] if u else "x" * 64)
        with self.lock:
            if ok and u:
                self.fails.pop(key, None)
                return "ok"
            self.fails[key] = (cnt + 1, first)
        return "bad"

    # ---- sessions: base64(name|expiry).hmac ----
    def _secret(self):
        path = self.store.dir / ".secret"
        if not path.exists():
            self.store._write(path, secrets.token_bytes(32))
        return path.read_bytes()

    def make_token(self, name):
        payload = base64.urlsafe_b64encode(("%s|%d" % (name, time.time() + SESSION_SECS)).encode("utf-8")).decode().rstrip("=")
        sig = hmac.new(self._secret(), payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + sig

    def user_from_token(self, token):
        try:
            payload, sig = token.rsplit(".", 1)
            good = hmac.new(self._secret(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, good):
                return None
            name, exp = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8").rsplit("|", 1)
            if float(exp) < time.time() or name not in self.users():
                return None
            return name
        except Exception:
            return None


LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>外贸报价计算器 Export Quote Calculator</title>
<style>
:root{color-scheme:light dark}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f4f6fb;color:#1b2333;
font:16px/1.5 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
@media(prefers-color-scheme:dark){body{background:#0f1420;color:#e8ecf5}.box{background:#181f2f!important;border-color:#2a3350!important}input{background:#0f1420!important;color:#e8ecf5!important;border-color:#2a3350!important}}
.box{width:min(380px,calc(100% - 32px));background:#fff;border:1px solid #dfe4ee;border-radius:14px;padding:22px}
h1{font-size:1.25rem;margin:0 0 4px}.en{font-size:.8em;color:#6b7489}
label{display:block;font-weight:600;font-size:.85rem;margin-top:12px}
input{width:100%;box-sizing:border-box;padding:10px;font:inherit;border:1px solid #dfe4ee;border-radius:10px;margin-top:4px}
button{width:100%;margin-top:16px;padding:11px;font:inherit;font-weight:600;color:#fff;background:#2563eb;border:0;border-radius:10px;cursor:pointer}
#msg{color:#b42318;font-size:.88rem;min-height:1.3em;margin-top:10px}p{font-size:.85rem}
</style></head><body><form class="box" id="f">
<h1>外贸报价计算器 <span class="en">Export Quote Calculator</span></h1>
<p id="hint"></p>
<label for="u">用户名 <span class="en">Username</span></label><input id="u" autocomplete="username" required>
<label for="p">密码 <span class="en">Password</span></label><input id="p" type="password" autocomplete="current-password" required>
<div id="rep" hidden><label for="p2">再输入一次密码 <span class="en">Repeat password</span></label><input id="p2" type="password" autocomplete="new-password"></div>
<button id="go" type="submit"></button><div id="msg" role="alert"></div></form>
<script>
var MODE="__MODE__",$=function(i){return document.getElementById(i)};
var T={login:["请登录。","Please sign in.","登录 Sign in"],
setup:["首次使用：请创建管理员账号（密码至少 8 位）。","First run: create the administrator account (password of 8+ characters).","创建 Create"],
remote:["尚未创建管理员账号。请先在服务器本机的浏览器里打开本网站（地址用 127.0.0.1）完成首次设置。","No administrator yet. Open this site in a browser on the server itself (address 127.0.0.1) to finish the first-time setup.","-"]};
var t=T[MODE];$("hint").innerHTML=t[0]+' <span class="en">'+t[1]+'</span>';
$("go").textContent=t[2];if(MODE==="remote"){$("f").querySelectorAll("input,button").forEach(function(e){e.disabled=true})}
if(MODE==="setup")$("rep").hidden=false;
$("f").addEventListener("submit",function(e){e.preventDefault();
if(MODE==="setup"&&$("p").value!==$("p2").value){$("msg").textContent="两次密码不一致 Passwords do not match";return}
fetch(MODE==="setup"?"/api/setup":"/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("u").value.trim(),password:$("p").value})})
.then(function(r){return r.json().then(function(j){return[r.ok,j]})}).then(function(x){if(x[0])location.reload();else $("msg").textContent=x[1].error||"error"})
.catch(function(){$("msg").textContent="网络错误 Network error"})});
</script></body></html>"""


class Ctx:
    """Everything the request handler needs."""

    def __init__(self, store, auth, port, tls):
        self.store, self.auth, self.port, self.tls = store, auth, port, tls
        self.allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}


def make_handler(ctx):
    store, auth = ctx.store, ctx.auth

    class Handler(BaseHTTPRequestHandler):
        server_version = "QuoteStore/2.0"
        timeout = 30  # drop stalled connections

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # ---- helpers ----
        def _send(self, code, body=b"", ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, obj, extra=None):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

        def _err(self, code, msg):
            self._json(code, {"error": msg})

        def _safe(self):
            """Reject DNS-rebinding (bad Host, local mode) and cross-site writes (bad Origin)."""
            host = self.headers.get("Host", "")
            if auth is None and host not in ctx.allowed_hosts:
                self._err(403, "bad host")
                return False
            origin = self.headers.get("Origin")
            scheme = "https" if ctx.tls else "http"
            if self.command not in ("GET", "HEAD") and origin not in (None, scheme + "://" + host):
                self._err(403, "bad origin")
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            return self.rfile.read(n)

        def _json_body(self):
            obj = json.loads(self._body().decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("object expected")
            return obj

        def _route(self):
            return [unquote(p) for p in urlparse(self.path).path.strip("/").split("/")]

        def _user(self):
            """Logged-in user name; "" when login is not required; None when login is needed."""
            if auth is None:
                return ""
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return auth.user_from_token(v)
            return None

        def _is_loopback(self):
            return self.client_address[0] in LOOPBACK

        def _cookie(self, value, max_age):
            flags = "HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % max_age + ("; Secure" if ctx.tls else "")
            return {"Set-Cookie": "%s=%s; %s" % (COOKIE, value, flags)}

        # ---- GET ----
        def do_GET(self):
            if not self._safe():
                return
            parts = self._route()
            user = self._user()
            if parts in ([""], ["index.html"]):
                if user is None:
                    mode = "login" if auth.users() else ("setup" if self._is_loopback() else "remote")
                    return self._send(200, LOGIN_PAGE.replace("__MODE__", mode).encode("utf-8"), "text/html; charset=utf-8")
                page = RES / "index.html"
                if not page.is_file():
                    return self._send(500, b"index.html not found - run: node build.js", "text/plain")
                return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            if user is None:
                return self._err(401, "login required")
            if parts == ["api", "info"]:
                return self._json(200, {"dataDir": str(store.dir), "authRequired": auth is not None, "user": user,
                                        "admin": bool(user) and auth.is_admin(user), "canChangeFolder": auth is None})
            if len(parts) == 2 and parts[0] == "api" and parts[1] in KINDS:
                return self._json(200, store.list(parts[1]))
            if parts == ["api", "users"] and auth is not None:
                if not auth.is_admin(user):
                    return self._err(403, "admin only")
                return self._json(200, [{"name": n, "admin": bool(u.get("admin"))} for n, u in sorted(auth.users().items())])
            if len(parts) == 3 and parts[:2] == ["api", "files"] and FILE_RE.match(parts[2]):
                data = store.get_file(parts[2])
                if data is None:
                    return self._err(404, "not found")
                return self._send(200, data, MIME[Path(parts[2]).suffix])
            self._err(404, "not found")

        do_HEAD = do_GET

        # ---- PUT ----
        def do_PUT(self):
            if not self._safe():
                return
            user = self._user()
            if user is None:
                return self._err(401, "login required")
            parts = self._route()
            try:
                if len(parts) == 3 and parts[0] == "api" and parts[1] in KINDS and ID_RE.match(parts[2]):
                    obj = self._json_body()
                    if obj.get("id") != parts[2]:
                        return self._err(400, "id mismatch")
                    store.put_json(parts[1], parts[2], obj, user or None)
                    return self._json(200, {"ok": True})
                if len(parts) == 3 and parts[:2] == ["api", "files"] and FILE_RE.match(parts[2]):
                    store.put_file(parts[2], self._body())
                    return self._json(200, {"ok": True})
            except (ValueError, OSError) as e:
                return self._err(400, str(e))
            self._err(404, "not found")

        # ---- DELETE ----
        def do_DELETE(self):
            if not self._safe():
                return
            user = self._user()
            if user is None:
                return self._err(401, "login required")
            parts = self._route()
            if len(parts) == 3 and parts[0] == "api" and parts[1] in KINDS and ID_RE.match(parts[2]):
                if user:  # only the creator or an administrator may delete
                    rec = store.read_json(parts[1], parts[2]) or {}
                    if rec.get("createdBy") not in (None, user) and not auth.is_admin(user):
                        return self._err(403, "只有创建人或管理员可以删除 / only the creator or an admin can delete")
                try:
                    store.delete(parts[1], parts[2])
                except OSError as e:
                    return self._err(500, str(e))
                return self._json(200, {"ok": True})
            if len(parts) == 3 and parts[:2] == ["api", "users"] and auth is not None:
                if not auth.is_admin(user):
                    return self._err(403, "admin only")
                if parts[2] == user:
                    return self._err(400, "不能删除自己 / cannot delete yourself")
                try:
                    auth.delete_user(parts[2])
                except ValueError as e:
                    return self._err(400, str(e))
                return self._json(200, {"ok": True})
            self._err(404, "not found")

        # ---- POST ----
        def do_POST(self):
            if not self._safe():
                return
            parts = self._route()
            try:
                if parts == ["api", "login"] and auth is not None:
                    b = self._json_body()
                    name, pw = str(b.get("name", "")), str(b.get("password", ""))
                    res = auth.verify(name, pw, self.client_address[0])
                    if res == "locked":
                        return self._err(429, "尝试次数过多，请 10 分钟后再试 / too many attempts, try again in 10 minutes")
                    if res != "ok":
                        return self._err(401, "用户名或密码错误 / wrong username or password")
                    return self._json(200, {"ok": True}, self._cookie(auth.make_token(name), SESSION_SECS))
                if parts == ["api", "logout"]:
                    return self._json(200, {"ok": True}, self._cookie("", 0))
                if parts == ["api", "setup"] and auth is not None:
                    if auth.users() or not self._is_loopback():
                        return self._err(403, "setup is only possible once, from the server itself")
                    b = self._json_body()
                    auth.set_user(str(b.get("name", "")), str(b.get("password", "")), admin=True)
                    return self._json(200, {"ok": True})
                user = self._user()
                if user is None:
                    return self._err(401, "login required")
                if parts == ["api", "users"] and auth is not None:
                    if not auth.is_admin(user):
                        return self._err(403, "admin only")
                    b = self._json_body()
                    auth.set_user(str(b.get("name", "")), str(b.get("password", "")), admin=bool(b.get("admin")))
                    return self._json(200, {"ok": True})
                if parts == ["api", "password"] and auth is not None:
                    b = self._json_body()
                    if auth.verify(user, str(b.get("old", "")), self.client_address[0]) != "ok":
                        return self._err(400, "原密码不正确 / current password is wrong")
                    auth.set_user(user, str(b.get("new", "")))
                    return self._json(200, {"ok": True}, self._cookie(auth.make_token(user), SESSION_SECS))
                if parts == ["api", "config"] and auth is None:
                    folder = self._json_body().get("dataDir", "")
                    store.set_dir(folder)
                    save_config(store.dir)
                    return self._json(200, {"dataDir": str(store.dir), "authRequired": False, "user": "", "admin": False,
                                            "canChangeFolder": True})
            except (ValueError, OSError, AttributeError) as e:
                return self._err(400, str(e))
            self._err(404, "not found")

    return Handler


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("dataDir")
    except (OSError, ValueError, AttributeError):
        return None


def save_config(data_dir):
    try:
        CONFIG_FILE.write_text(json.dumps({"dataDir": str(data_dir)}, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print("warning: could not remember the folder (%s)" % e, file=sys.stderr)


def say(*lines):
    """print() that never crashes on a console that cannot show Chinese."""
    for line in lines:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)


def fail(msg):
    """Report a fatal problem; when double-clicked, keep the window open so it can be read."""
    say(msg)
    if FROZEN and sys.stdin and sys.stdin.isatty():
        try:
            input("按回车键退出 Press Enter to exit...")
        except (EOFError, OSError):
            pass
    sys.exit(1)


def already_running(url):
    """True if a copy of this server is already answering on url."""
    try:
        import ssl as _ssl
        from urllib.request import urlopen
        ctx = _ssl._create_unverified_context() if url.startswith("https") else None
        with urlopen(url + "api/info", timeout=2, context=ctx):
            return True
    except Exception as e:
        return getattr(e, "code", None) == 401      # answered, but wants a login


def open_store(args):
    chosen = args.data_dir or os.environ.get("QUOTE_DATA_DIR") or load_config() or str(DEFAULT_DIR)
    try:
        return Store(chosen)
    except (ValueError, OSError) as e:
        fail("无法使用数据文件夹 Cannot use data folder %r: %s" % (chosen, e))


def user_cli(argv):
    ap = argparse.ArgumentParser(prog="server.py user", description="Manage accounts (server mode)")
    ap.add_argument("cmd", choices=["list", "add", "passwd", "del"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--admin", action="store_true")
    ap.add_argument("--data-dir")
    args = ap.parse_args(argv)
    auth = Auth(open_store(args))
    try:
        if args.cmd == "list":
            for n, u in sorted(auth.users().items()):
                say("%s%s" % (n, "  (admin)" if u.get("admin") else ""))
        elif not args.name:
            fail("需要用户名 a user name is required")
        elif args.cmd == "del":
            auth.delete_user(args.name)
            say("deleted " + args.name)
        else:
            pw = getpass.getpass("Password (8+ characters): ")
            if pw != getpass.getpass("Repeat password: "):
                fail("两次密码不一致 passwords differ")
            auth.set_user(args.name, pw, admin=True if args.admin else None)
            say("saved " + args.name)
    except ValueError as e:
        fail(str(e))


def backup_loop(store):
    while True:
        try:
            store.backup()
        except OSError as e:
            say("backup failed: %s" % e)
        time.sleep(3600)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "user":
        return user_cli(sys.argv[2:])
    ap = argparse.ArgumentParser(description="Export Quote Calculator - storage server")
    ap.add_argument("--data-dir", help="folder for records and generated files (remembered for next time)")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = reachable from the network (turns login on)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--auth", action="store_true", help="require login even on 127.0.0.1")
    ap.add_argument("--cert", help="TLS certificate (PEM) to serve HTTPS")
    ap.add_argument("--key", help="TLS private key (PEM)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = ap.parse_args()

    if bool(args.cert) != bool(args.key):
        fail("--cert 和 --key 必须同时提供 --cert and --key go together")
    shared = args.auth or args.host not in LOOPBACK
    tls = bool(args.cert and args.key)
    url = "%s://%s:%d/" % ("https" if tls else "http", "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host, args.port)

    store = open_store(args)
    if args.data_dir:
        save_config(store.dir)
    auth = Auth(store) if shared else None
    ctx = Ctx(store, auth, args.port, tls)

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), make_handler(ctx))
        httpd.daemon_threads = True
        if tls:
            sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            sctx.load_cert_chain(args.cert, args.key)
            httpd.socket = sctx.wrap_socket(httpd.socket, server_side=True)
    except (OSError, ssl.SSLError) as e:
        if not isinstance(e, ssl.SSLError) and already_running(url):
            say("程序已在运行，正在打开页面。 Already running - opening the page.")
            if not args.no_browser:
                webbrowser.open(url)
            return
        fail("无法启动 Cannot start on port %d: %s (port in use? try --port 9000)" % (args.port, e))

    lines = ["外贸报价计算器 Export Quote Calculator",
             "  网址 Open:        " + url,
             "  数据文件夹 Data:  " + str(store.dir)]
    if shared:
        lines.append("  多人模式：需要登录；数据每天自动备份到 backups 文件夹。 Shared mode: login required; daily backups in 'backups'.")
        if not auth.users():
            lines.append("  ** 尚无账号：请在本机浏览器打开上面的网址创建管理员。 No accounts yet: open the address above on this machine to create the administrator. **")
        threading.Thread(target=backup_loop, args=(store,), daemon=True).start()
    lines.append("  使用期间请保持此窗口打开；关闭此窗口即退出。 Keep this window open while using; close it to quit.")
    say(*lines)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        say("\n已停止 Stopped.")


if __name__ == "__main__":
    main()
