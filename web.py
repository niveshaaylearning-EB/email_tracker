"""Password-protected web server for the mailbox dashboard.

Every page needs the DASHBOARD_PASSWORD (browser sign-in box; any user name, or
DASHBOARD_USER if set). Only /health answers without it, for Easypanel's checks.

  /                     the dashboard
  /data/threads.json    conversations (built by tracker.py)
  /data/daily.json      emails per day
  /status               when the data was last refreshed
  /admin                upload the laptop's emails_master.jsonl once (older mail), refresh now
"""
import base64
import hmac
import json
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
WEB = HERE / "web"
MAX_UPLOAD = 100 * 1024 * 1024
STATIC = {"/admin": ("admin.html", "text/html; charset=utf-8"),
          "/dash.js": ("dash.js", "application/javascript; charset=utf-8")}

log = logging.getLogger("web")


class Handler(BaseHTTPRequestHandler):
    tracker_lock: threading.Lock = None
    refresh = None          # callable that collects new mail and rebuilds
    tracker_data: Path = None

    def log_message(self, fmt, *args):  # keep the logs to errors only
        pass

    # ----- helpers -----
    def _authorised(self):
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not password:
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        want_user = os.environ.get("DASHBOARD_USER", "")
        user_ok = not want_user or hmac.compare_digest(user, want_user)
        return user_ok and hmac.compare_digest(given.encode(), password.encode())

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
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
        self.wfile.write(body)

    def _gate(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
            return False
        if not os.environ.get("DASHBOARD_PASSWORD"):
            self._send(503, "Set DASHBOARD_PASSWORD in Easypanel to open the dashboard.", "text/plain; charset=utf-8")
            return False
        if not self._authorised():
            self._send(401, "Sign in required", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="Mailbox dashboard", charset="UTF-8"'})
            return False
        return True

    # ----- routes -----
    def do_GET(self):
        if not self._gate():
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            # The same page as the claude.ai dashboard (page.html), inside a standalone shell
            page = (WEB / "shell.html").read_text(encoding="utf-8").replace(
                "<!--PAGE-->", (WEB / "page.html").read_text(encoding="utf-8"))
            return self._send(200, page, "text/html; charset=utf-8")
        if path in STATIC:
            name, ctype = STATIC[path]
            f = WEB / name
            return self._send(200, f.read_bytes(), ctype) if f.exists() else self._send(404, "Not found", "text/plain")
        if path in ("/data/threads.json", "/data/daily.json"):
            f = self.tracker_data / "dashboard" / path.rsplit("/", 1)[1]
            if not f.exists():
                return self._send(404, {"error": "No data yet: the first collection is still running"})
            return self._send(200, f.read_bytes())
        if path == "/status":
            try:
                state = json.loads((self.tracker_data / "state.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                state = {}
            state["busy"] = self.tracker_lock.locked()
            return self._send(200, state)
        self._send(404, "Not found", "text/plain")

    def do_POST(self):
        if not self._gate():
            return
        path = self.path.split("?")[0]
        if path == "/admin/refresh":
            if self.tracker_lock.locked():
                return self._send(409, {"message": "A refresh is already running."})
            threading.Thread(target=self.refresh, daemon=True).start()
            return self._send(202, {"message": "Refresh started. Reload the dashboard in a few minutes."})
        if path == "/admin/seed":
            return self._seed()
        self._send(404, "Not found", "text/plain")

    def _seed(self):
        """Add the laptop's permanent record (emails_master.jsonl). Existing emails are never replaced."""
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            return self._send(400, {"message": "Choose an emails_master.jsonl file (up to 100 MB)."})
        text = self.rfile.read(length).decode("utf-8", errors="replace")
        good, bad = [], 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(row, dict) and row.get("mid") and row.get("received"):
                good.append(line)
            else:
                bad += 1
        if not good:
            return self._send(400, {"message": "No emails found in that file. Is it emails_master.jsonl?"})
        raw = self.tracker_data / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / f"seed_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.jsonl").write_text("\n".join(good) + "\n", encoding="utf-8")
        threading.Thread(target=self.refresh, daemon=True).start()
        self._send(200, {"message": f"Received {len(good)} emails ({bad} lines skipped). The dashboard is being rebuilt; reload it in a minute."})


def serve(lock, refresh, tracker_data, port=None):
    Handler.tracker_lock, Handler.refresh, Handler.tracker_data = lock, staticmethod(refresh), Path(tracker_data)
    port = int(port or os.environ.get("PORT", "8080"))
    if not os.environ.get("DASHBOARD_PASSWORD"):
        log.warning("DASHBOARD_PASSWORD is not set: the dashboard stays closed until it is")
    log.info("Dashboard on port %d", port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
