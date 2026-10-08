#!/usr/bin/env python3
"""Local storage server for the Export Quote Calculator (Python standard library only).

Serves index.html and keeps every record in a folder you choose:

    <data folder>/
        items/<id>.json      quote-list entries
        quotes/<id>.json     generated-quotation records (search metadata)
        files/<name>.xlsx    the generated Excel / PDF files themselves

Usage:
    python server.py                       # uses the remembered / default folder
    python server.py --data-dir D:\\Quotes  # choose the folder (remembered in config.json)
    python server.py --port 9000 --no-browser

The server only listens on 127.0.0.1 and rejects requests from other web pages.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
DEFAULT_DIR = ROOT / "quote_data"
MAX_BODY = 64 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,149}\.(xlsx|pdf)$")
MIME = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
}
KINDS = ("items", "quotes")


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

    def list(self, kind):
        out = []
        for f in sorted((self.dir / kind).glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue  # skip unreadable / half-edited files
        return out

    def put_json(self, kind, rid, obj):
        with self.lock:
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


def make_handler(store, port):
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class Handler(BaseHTTPRequestHandler):
        server_version = "QuoteStore/1.0"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # ---- helpers ----
        def _send(self, code, body=b"", ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _safe(self):
            """Reject DNS-rebinding (bad Host) and cross-site writes (bad Origin)."""
            if self.headers.get("Host", "") not in allowed_hosts:
                self._json(403, {"error": "bad host"})
                return False
            origin = self.headers.get("Origin")
            if self.command not in ("GET", "HEAD") and origin not in (None, "http://" + self.headers.get("Host", "")):
                self._json(403, {"error": "bad origin"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            return self.rfile.read(n)

        def _route(self):
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/")]
            return parts

        # ---- verbs ----
        def do_GET(self):
            if not self._safe():
                return
            parts = self._route()
            if parts in ([""], ["index.html"]):
                page = ROOT / "index.html"
                if not page.is_file():
                    return self._send(500, b"index.html not found - run: node build.js", "text/plain")
                return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            if parts == ["api", "info"]:
                return self._json(200, {"dataDir": str(store.dir)})
            if len(parts) == 2 and parts[0] == "api" and parts[1] in KINDS:
                return self._json(200, store.list(parts[1]))
            if len(parts) == 3 and parts[:2] == ["api", "files"] and FILE_RE.match(parts[2]):
                data = store.get_file(parts[2])
                if data is None:
                    return self._json(404, {"error": "not found"})
                return self._send(200, data, MIME[Path(parts[2]).suffix])
            self._json(404, {"error": "not found"})

        do_HEAD = do_GET

        def do_PUT(self):
            if not self._safe():
                return
            parts = self._route()
            try:
                if len(parts) == 3 and parts[0] == "api" and parts[1] in KINDS and ID_RE.match(parts[2]):
                    obj = json.loads(self._body().decode("utf-8"))
                    if not isinstance(obj, dict) or obj.get("id") != parts[2]:
                        return self._json(400, {"error": "id mismatch"})
                    store.put_json(parts[1], parts[2], obj)
                    return self._json(200, {"ok": True})
                if len(parts) == 3 and parts[:2] == ["api", "files"] and FILE_RE.match(parts[2]):
                    store.put_file(parts[2], self._body())
                    return self._json(200, {"ok": True})
            except (ValueError, OSError) as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def do_DELETE(self):
            if not self._safe():
                return
            parts = self._route()
            if len(parts) == 3 and parts[0] == "api" and parts[1] in KINDS and ID_RE.match(parts[2]):
                try:
                    store.delete(parts[1], parts[2])
                except OSError as e:
                    return self._json(500, {"error": str(e)})
                return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._safe():
                return
            if self._route() == ["api", "config"]:
                try:
                    folder = json.loads(self._body().decode("utf-8")).get("dataDir", "")
                    store.set_dir(folder)
                    save_config(store.dir)
                except (ValueError, OSError, AttributeError) as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, {"dataDir": str(store.dir)})
            self._json(404, {"error": "not found"})

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


def main():
    ap = argparse.ArgumentParser(description="Export Quote Calculator - local storage server")
    ap.add_argument("--data-dir", help="folder for records and generated files (remembered for next time)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = ap.parse_args()

    chosen = args.data_dir or os.environ.get("QUOTE_DATA_DIR") or load_config() or str(DEFAULT_DIR)
    try:
        store = Store(chosen)
    except (ValueError, OSError) as e:
        sys.exit("Cannot use data folder %r: %s (it must be an absolute, writable path)" % (chosen, e))
    if args.data_dir:
        save_config(store.dir)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, args.port))
    except OSError as e:
        sys.exit("Cannot listen on port %d: %s (try --port 9000)" % (args.port, e))
    url = "http://127.0.0.1:%d/" % args.port
    print("Export Quote Calculator")
    print("  Open:        " + url)
    print("  Data folder: " + str(store.dir))
    print("  Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
