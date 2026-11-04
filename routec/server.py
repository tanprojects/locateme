"""routecd: the Route C daemon. HTTP JSON API on 127.0.0.1, token required.

Run by launchd (see install.sh) or `routec daemon`. One instance per Mac: it owns the Elgato
stream and the KMBox serial port; everything else talks to it.
"""
import io
import json
import logging
import logging.handlers
import os
import secrets
import signal
import socketserver
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import config as config_mod
from . import lease as leases
from .capture import CaptureError
from .engine import Engine, to_box
from .jobs import DONE, JobManager

VIEW_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Route C live view</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#111;--fg:#eee;--mute:#999;--ok:#3c3;--bad:#e55;--warn:#eb3}
body{margin:0;background:var(--bg);color:var(--fg);font:14px -apple-system,system-ui,sans-serif}
header{padding:8px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
#verdict{font-weight:600}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
img{width:100%;display:block;background:#000}small{color:var(--mute)}
pre{margin:0;padding:8px 16px;color:var(--mute);white-space:pre-wrap;font-size:12px}
</style></head><body>
<header><span id="verdict">connecting...</span><small id="meta"></small></header>
<img id="f" alt="laptop screen as Route C sees it">
<pre id="jobs"></pre>
<script>
const t=new URLSearchParams(location.search).get('t')||'';
const img=document.getElementById('f');
function frame(){const n=new Image();n.onload=()=>{img.src=n.src;setTimeout(frame,800)};
 n.onerror=()=>setTimeout(frame,2000);n.src='/frame.jpg?width=1600&t='+t+'&_='+Date.now();}
async function stat(){try{const r=await fetch('/status?t='+t);const s=await r.json();
 const v=document.getElementById('verdict');v.textContent=s.verdict;
 v.className=s.verdict.startsWith('OK')?'ok':(s.verdict.startsWith('NO PICTURE')?'bad':'warn');
 document.getElementById('meta').textContent='presence: '+s.presence.verdict+' | lease: '+s.lease;
 const j=await (await fetch('/jobs?limit=8&t='+t)).json();
 document.getElementById('jobs').textContent=j.jobs.reverse().map(x=>new Date(x.created*1000).toLocaleTimeString()+'  '+x.status.padEnd(11)+x.kind.padEnd(10)+(x.owner||'')+(x.error?'  '+x.error:'')).join('\\n');
}catch(e){document.getElementById('verdict').textContent='daemon not reachable';}
 setTimeout(stat,2000);}
frame();stat();
</script></body></html>"""


def setup_logging(cfg):
    os.makedirs(cfg["log_dir"], exist_ok=True)
    logger = logging.getLogger("routec")
    logger.setLevel(logging.INFO)
    h = logging.handlers.RotatingFileHandler(os.path.join(cfg["log_dir"], "routecd.log"),
                                             maxBytes=2_000_000, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(h)
    if sys.stderr.isatty():
        logger.addHandler(logging.StreamHandler())
    return lambda *a: logger.info(" ".join(str(x) for x in a))


def ensure_token(cfg):
    path = cfg["token_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(16))
    return open(path).read().strip()


class LocalHTTP(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse-mDNS lookup that hangs 30 s+
        # on this Mac (route doc, 21 Sep 2026). Bind without it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


class App:
    def __init__(self, cfg, engine=None, log=None):
        self.cfg = cfg
        self.log = log or (lambda *a: None)
        self.token = ensure_token(cfg)
        self.engine = engine or Engine(cfg, self.log)
        self.jobs = JobManager(cfg, self.engine.run_job, self.log)

    # Each handler returns (status, dict) or (status, bytes, content_type).
    def frame_meta_warn(self, f):
        h = f.health or {}
        if h.get("state") != "live":
            return "laptop picture is %s: %s" % (h.get("state"), h.get("detail"))
        return None

    def read(self, f, payload):
        return {"frame": f.meta(), "warning": self.frame_meta_warn(f), **payload}

    def h_status(self, q, body):
        st = self.engine.status()
        st["version"] = __version__
        st["jobs_busy"] = self.jobs.busy()
        if (body or {}).get("deep") or q.get("deep"):
            try:
                f = self.engine.frame(fresh=True)
                locked, why = self.engine.locked_check(f)
                st["locked"] = {"locked": locked, "why": why}
            except CaptureError as e:
                st["locked"] = {"error": str(e)}
        return 200, st

    def h_screen(self, q, b):
        f = self.engine.frame(fresh=bool(b.get("fresh")))
        _, lines, engine = self.engine.lines(to_box(b.get("box")), f)
        return 200, self.read(f, {"engine": engine, "lines": [
            {"cx": l["x"] + l["w"] // 2, "cy": l["y"] + l["h"] // 2, "text": l["text"]} for l in lines]})

    def h_find(self, q, b):
        f = self.engine.frame(fresh=bool(b.get("fresh")))
        _, hits, engine = self.engine.find(b["text"], to_box(b.get("box")), f)
        return 200, self.read(f, {"engine": engine, "hits": [
            {"cx": h["cx"], "cy": h["cy"], "text": h["text"], "line": h["line"][:100]} for h in hits]})

    def h_ocr(self, q, b):
        f = self.engine.frame(fresh=bool(b.get("fresh")))
        text, engine = self.engine.ocr.text(f.image(), to_box(b.get("box")))
        return 200, self.read(f, {"engine": engine, "text": text})

    def h_look(self, q, b):
        f = self.engine.frame(fresh=bool(b.get("fresh", True)))
        fmt = b.get("fmt", "jpg")
        paths = [self.engine.snaps.save(f, to_box(b.get("box")), b.get("width", 1600), "look", fmt)]
        for c in b.get("crops") or []:
            paths.append(self.engine.snaps.save(f, to_box(c), None, "crop", fmt))
        return 200, self.read(f, {"paths": paths})

    def h_grab(self, q, b):
        f = self.engine.frame(fresh=bool(b.get("fresh", True)))
        img = f.image()
        box = to_box(b.get("box"))
        if box:
            img = img.crop(box)
        path = os.path.abspath(os.path.expanduser(b["path"]))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img.save(path)
        return 200, self.read(f, {"path": path, "size": list(img.size)})

    def h_wait(self, q, b):
        box = to_box(b.get("box"))
        if b.get("change"):
            r = self.engine.wait_change(box, b.get("timeout", 10))
        else:
            r = self.engine.wait_text(b["text"], box, b.get("timeout", 15), bool(b.get("gone")))
        return 200, r

    def h_presence(self, q, b):
        w = (b or {}).get("window") or (q.get("window") or [None])[0]
        return 200, self.engine.presence.report(float(w) if w else None)

    def h_lease(self, q, b):
        try:
            rec = leases.take(self.cfg["lease_file"], b.get("owner"), b.get("minutes", 15),
                              bool(b.get("unattended")), self.cfg["unattended_cutoff"])
            return 200, {"ok": True, "lease": rec}
        except leases.LeaseError as e:
            return 409, {"ok": False, "error": str(e), "code": "lease"}

    def h_release(self, q, b):
        try:
            if self.jobs.busy():
                active = [j for j in self.jobs.recent(50) if j["status"] in ("queued", "running")
                          and j["owner"] == b.get("owner")]
                for j in active:
                    self.jobs.cancel(j["id"])
            return 200, {"ok": True, "result": leases.release(self.cfg["lease_file"], b.get("owner"))}
        except leases.LeaseError as e:
            return 409, {"ok": False, "error": str(e), "code": "lease"}

    def h_submit(self, q, b):
        kind = b.get("kind")
        if kind not in ("click", "rclick", "dclick", "move", "hover", "type", "key", "wheel", "click_text",
                        "batch", "calibrate", "wake"):
            return 400, {"error": "unknown job kind %r" % kind}
        params = b.get("params") or {}
        if kind == "batch":
            err = self.engine.validate_steps(params.get("steps"))
            if err:
                return 400, {"error": err + ". Nothing was sent.", "code": "bad_batch"}
        try:
            job, existing = self.jobs.submit(kind, params, b.get("owner") or "anonymous", b.get("id"))
        except ValueError as e:
            return 409, {"error": str(e), "code": "id_reused"}
        job = self.jobs.wait(job["id"], float(b.get("wait", 0))) if job["status"] not in DONE else job
        return 200, {"job": job, "existing": existing}

    def h_job(self, q, b, job_id):
        j = self.jobs.wait(job_id, float((q.get("wait") or [0])[0]))
        if j is None:
            return 404, {"error": "no such job %r (the journal keeps the last 500)" % job_id}
        return 200, {"job": j}

    def h_cancel(self, q, b, job_id):
        j = self.jobs.cancel(job_id)
        return (200, {"job": j}) if j else (404, {"error": "no such job"})

    def h_jobs(self, q, b):
        return 200, {"jobs": self.jobs.recent(int((q.get("limit") or [20])[0]))}

    def h_restart(self, q, b):
        self.engine.capture.restart()
        self.engine.presence.reset()
        return 200, {"ok": True}

    def h_frame(self, q, b):
        f = self.engine.frame(fresh=bool(q.get("fresh")))
        img = f.image()
        box = (q.get("box") or [None])[0]
        if box:
            img = img.crop(to_box([int(v) for v in box.split(",")]))
        w = int((q.get("width") or [0])[0] or 0)
        if w and img.width > w:
            img = img.resize((w, max(1, img.height * w // img.width)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return 200, buf.getvalue(), "image/jpeg"


ROUTES = {
    ("GET", "/status"): "h_status", ("POST", "/status"): "h_status",
    ("POST", "/screen"): "h_screen", ("POST", "/find"): "h_find", ("POST", "/ocr"): "h_ocr",
    ("POST", "/look"): "h_look", ("POST", "/grab"): "h_grab", ("POST", "/wait"): "h_wait",
    ("GET", "/presence"): "h_presence", ("POST", "/presence"): "h_presence",
    ("POST", "/lease"): "h_lease", ("POST", "/release"): "h_release",
    ("POST", "/jobs"): "h_submit", ("GET", "/jobs"): "h_jobs",
    ("POST", "/capture/restart"): "h_restart", ("GET", "/frame.jpg"): "h_frame",
}


def make_handler(app):
    port = app.cfg["port"]
    hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class H(BaseHTTPRequestHandler):
        server_version = "routecd/" + __version__
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if self.headers.get("Host") not in hosts:
                return self._send(403, {"error": "bad Host header"})
            if url.path == "/ping":
                return self._send(200, {"ok": True, "version": __version__})
            if url.path in ("/", "/view"):
                if (q.get("t") or [""])[0] != app.token:
                    return self._send(403, b"token required: run `routec view`", "text/plain")
                return self._send(200, VIEW_HTML.encode(), "text/html; charset=utf-8")
            tok = self.headers.get("X-RouteC-Token") or (q.get("t") or [""])[0]
            if not secrets.compare_digest(tok, app.token):
                return self._send(401, {"error": "missing or wrong token"})
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    if "json" not in (self.headers.get("Content-Type") or ""):
                        return self._send(415, {"error": "JSON only"})
                    try:
                        body = json.loads(self.rfile.read(n))
                    except ValueError:
                        return self._send(400, {"error": "bad JSON"})
            try:
                parts = url.path.strip("/").split("/")
                if parts[0] == "jobs" and len(parts) >= 2:
                    if len(parts) == 3 and parts[2] == "cancel" and method == "POST":
                        r = app.h_cancel(q, body, parts[1])
                    elif method == "GET":
                        r = app.h_job(q, body, parts[1])
                    else:
                        return self._send(404, {"error": "not found"})
                else:
                    name = ROUTES.get((method, url.path))
                    if not name:
                        return self._send(404, {"error": "not found: %s %s" % (method, url.path)})
                    r = getattr(app, name)(q, body)
                self._send(*r)
            except CaptureError as e:
                self._send(503, {"error": str(e), "code": "capture_" + e.code, "hint": e.hint})
            except KeyError as e:
                self._send(400, {"error": "missing field %s" % e})
            except Exception as e:
                app.log("handler error: %s" % traceback.format_exc())
                self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return H


def serve(cfg=None, engine=None):
    cfg = cfg or config_mod.load()
    log = setup_logging(cfg)
    try:
        httpd = LocalHTTP((cfg["host"], cfg["port"]), None)
    except OSError as e:
        print("routecd: cannot listen on %s:%d (%s). Is it already running? `routec status`"
              % (cfg["host"], cfg["port"], e), file=sys.stderr)
        return 3
    app = App(cfg, engine, log)
    httpd.RequestHandlerClass = make_handler(app)
    pidfile = os.path.join(cfg["state_dir"], "routecd.pid")
    with open(pidfile, "w") as fh:
        fh.write(str(os.getpid()))
    log("routecd %s listening on %s:%d (camera_auth=%s)" % (__version__, cfg["host"], cfg["port"],
                                                            app.engine.capture.camera_auth))

    def stop(*_):
        log("routecd stopping")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        app.engine.capture.stop()
        app.engine.km.close()
        try:
            os.unlink(pidfile)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(serve())
