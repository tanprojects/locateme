"""routec: the command line for Route C. Standard library only, so it starts fast from any shell.

READ (safe any time; no lease needed)
  routec status [--deep]            one-line verdict: can Claude see the laptop, and if not, why
  routec doctor                     full diagnosis with the fix for anything wrong
  routec screen [X Y W H]           OCR lines as "cx cy  text" (cx,cy = click point)
  routec find TEXT [--box X Y W H]  where TEXT is on screen -> [{"cx","cy","text","line"}]
  routec ocr X Y W H                text of one region
  routec look [X Y W H] [--width N] [--crop X Y W H ...]   save a JPEG to view; prints the path
  routec grab OUT.png [X Y W H]     full-resolution PNG
  routec wait TEXT [--gone] [--timeout S] [--box X Y W H]  until TEXT appears (or goes)
  routec presence                   is someone using the laptop?
  routec view [--open]              live view URL for a browser (what Claude sees)

INPUT (needs the lease; refused if someone is using the laptop or the picture is not live)
  routec lease OWNER [MINUTES] [--unattended]     routec release OWNER
  routec click X Y [--right|--double] [--ctrl|--shift]
  routec click-text TEXT [--index N] [--box X Y W H] [--offset DX DY]
  routec hover X Y                  move only
  routec type TEXT                  any ASCII; long text is fine (runs in the daemon)
  routec key KEY [KEY...]           e.g. key ctr-l a | key enter | key back
  routec wheel X Y N [--repeat K]
  routec run STEPS.json|-           a batch in one job (see README for step types)
  routec wake                       nudge the mouse to wake a black/asleep screen
  routec calibrate                  re-measure the pointer scale

JOBS  (every input command is a job; an error never means "did not happen")
  routec job ID [--wait S]          status/result of a job. Asking again never re-runs it.
  routec jobs                       recent jobs     routec cancel ID

DAEMON
  routec start [--here] | stop | restart | restart-capture | logs [N] | daemon (foreground)

Common options: --owner SLUG (or $ROUTEC_OWNER / $KVM_OWNER), --id ID (idempotency key),
--wait S (how long to wait for a job, default 40), --async (do not wait), --json, --fresh.
Exit codes: 0 ok, 1 failed/refused, 2 usage, 3 still running, 4 daemon unreachable, 5 no picture.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from . import config as config_mod

LABEL = "com.ekorre.routec"
DAEMON_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"


class Unreachable(Exception):
    pass


class Client:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = "http://127.0.0.1:%d" % cfg["port"]
        self._token = None

    def token(self):
        if self._token is None:
            try:
                self._token = open(self.cfg["token_file"]).read().strip()
            except OSError:
                self._token = ""
        return self._token

    def call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-RouteC-Token", self.token())
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except ValueError:
                return e.code, {"error": str(e)}
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise Unreachable(str(getattr(e, "reason", e)))

    def ping(self):
        try:
            s, b = self.call("GET", "/ping", timeout=3)
            return s == 200
        except Unreachable:
            return False


# ── daemon management ───────────────────────────────────────────────────────────────────────
def _uid():
    return os.getuid()


def agent_plist():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def agent_loaded():
    r = subprocess.run(["launchctl", "print", "gui/%d/%s" % (_uid(), LABEL)], capture_output=True)
    return r.returncode == 0


def daemon_python(cfg):
    p = cfg.get("daemon_python") or DAEMON_PYTHON
    return p if os.path.exists(p) else sys.executable


def spawn_here(cfg):
    """Start the daemon as a detached child of this shell. It inherits this shell's Camera
    permission, which is the point: use it when the launchd agent has none yet."""
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(cfg["log_dir"], exist_ok=True)
    out = open(os.path.join(cfg["log_dir"], "routecd.stdout.log"), "a")
    env = dict(os.environ, PYTHONPATH=pkg_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.Popen([daemon_python(cfg), "-m", "routec.server"], cwd=pkg_root, env=env, stdout=out,
                     stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)


def start(cfg, here=False, quiet=False):
    c = Client(cfg)
    if c.ping():
        return True
    if here:
        if os.path.exists(agent_plist()) and agent_loaded():
            subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (_uid(), LABEL)], capture_output=True)
            time.sleep(1)
        spawn_here(cfg)
    elif os.path.exists(agent_plist()):
        if not agent_loaded():
            subprocess.run(["launchctl", "bootstrap", "gui/%d" % _uid(), agent_plist()], capture_output=True)
        subprocess.run(["launchctl", "kickstart", "gui/%d/%s" % (_uid(), LABEL)], capture_output=True)
    else:
        spawn_here(cfg)
    for _ in range(40):
        time.sleep(0.25)
        if c.ping():
            if not quiet:
                print("routecd started", file=sys.stderr)
            return True
    return False


def stop(cfg):
    if os.path.exists(agent_plist()) and agent_loaded():
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (_uid(), LABEL)], capture_output=True)
    pidfile = os.path.join(cfg["state_dir"], "routecd.pid")
    try:
        pid = int(open(pidfile).read())
        os.kill(pid, 15)
    except (OSError, ValueError):
        pass
    for _ in range(20):
        if not Client(cfg).ping():
            return True
        time.sleep(0.25)
    return False


# ── argument helpers ────────────────────────────────────────────────────────────────────────
class Args:
    def __init__(self, argv):
        self.flags = {}
        self.pos = []
        multi = {"--crop"}
        takes = {"--owner": 1, "--id": 1, "--wait": 1, "--box": 4, "--width": 1, "--timeout": 1,
                 "--index": 1, "--offset": 2, "--repeat": 1, "--crop": 4, "--minutes": 1}
        i = 0
        while i < len(argv):
            a = argv[i]
            if a in takes:
                n = takes[a]
                vals = argv[i + 1:i + 1 + n]
                if len(vals) < n:
                    raise SystemExit(usage("%s needs %d value(s)" % (a, n)))
                v = vals if n > 1 else vals[0]
                if a in multi:
                    self.flags.setdefault(a, []).append(v)
                else:
                    self.flags[a] = v
                i += 1 + n
            elif a.startswith("--") and len(a) > 2:
                self.flags[a] = True
                i += 1
            else:
                self.pos.append(a)
                i += 1

    def get(self, k, default=None):
        return self.flags.get(k, default)


def usage(msg):
    sys.stderr.write("routec: %s\n(run `routec help` for usage)\n" % msg)
    return 2


def ints(vals, n, what):
    if len(vals) != n:
        raise SystemExit(usage("%s needs %d numbers" % (what, n)))
    try:
        return [int(float(v)) for v in vals]
    except ValueError:
        raise SystemExit(usage("%s must be numbers" % what))


def owner_of(a):
    return a.get("--owner") or os.environ.get("ROUTEC_OWNER") or os.environ.get("KVM_OWNER")


def out(obj, as_json=False):
    if as_json or not isinstance(obj, str):
        print(json.dumps(obj, indent=None if as_json else 1, default=str))
    else:
        print(obj)
    sys.stdout.flush()


def warn_frame(resp):
    w = resp.get("warning")
    if w:
        print("WARNING: %s" % w, file=sys.stderr)


def fail(status, body):
    code = body.get("code") or ""
    msg = body.get("error") or json.dumps(body)
    print("routec: %s%s" % (msg, (" [%s]" % code) if code else ""), file=sys.stderr)
    if body.get("hint"):
        print("hint: %s" % body["hint"], file=sys.stderr)
    return 5 if code.startswith("capture_") else 1


# ── job submission ──────────────────────────────────────────────────────────────────────────
def submit(c, a, kind, params):
    owner = owner_of(a)
    if not owner:
        return usage("input needs an owner: --owner SLUG or export ROUTEC_OWNER=SLUG (and take the lease)")
    jid = a.get("--id") or uuid.uuid4().hex[:12]
    wait = 0.0 if a.get("--async") else float(a.get("--wait", 40))
    print("job %s (%s) submitted; if this call is cut off, ask `routec job %s` - never resend" % (jid, kind, jid),
          file=sys.stderr)
    sys.stderr.flush()
    s, b = c.call("POST", "/jobs", {"kind": kind, "params": params, "owner": owner, "id": jid, "wait": wait},
                  timeout=wait + 15)
    if s != 200:
        return fail(s, b)
    return report_job(b["job"], a, existing=b.get("existing"))


def report_job(j, a, existing=False):
    if existing:
        print("(job %s already existed; it was NOT run again)" % j["id"], file=sys.stderr)
    st = j["status"]
    if a.get("--json"):
        out(j, True)
    elif st == "done":
        out({"ok": True, "job": j["id"], "result": j["result"]})
    elif st in ("queued", "running"):
        out({"ok": None, "job": j["id"], "status": st, "progress": j.get("progress"),
             "next": "routec job %s --wait 30" % j["id"]})
        return 3
    else:
        out({"ok": False, "job": j["id"], "status": st, "error": j.get("error"), "code": j.get("error_code"),
             "may_have_executed": j.get("may_have_executed"), "partial": j.get("result")})
    return 0 if st == "done" else (1 if st not in ("queued", "running") else 3)


def click_params(a):
    x, y = ints(a.pos[:2], 2, "click")
    p = {"x": x, "y": y}
    mods = []
    if a.get("--ctrl"):
        mods.append("ctr-l")
    if a.get("--shift"):
        mods.append("shift-l")
    if mods:
        p["mods"] = mods
    return p


def common_params(a, p):
    if a.get("--allow-letterbox"):
        p["allow_letterbox"] = True
    if a.get("--no-verify"):
        p["verify"] = False
    return p


# ── doctor ──────────────────────────────────────────────────────────────────────────────────
def doctor(cfg, c):
    rows = []

    def row(level, item, detail):
        rows.append((level, item, detail))
        print("%-4s %-16s %s" % (level, item, detail))
        sys.stdout.flush()

    import platform
    row("OK" if platform.system() == "Darwin" else "FAIL", "platform",
        platform.system() + ("" if platform.system() == "Darwin" else
                             ": this is not the Mac. Route C only runs on the Mac (Desktop Commander, not device_bash)"))
    app = os.path.expanduser("~/Applications/RouteC.app")
    if os.path.exists(app):
        # Ask the app itself (via `open`), not a child of this shell, or the answer would be this
        # shell's own permission.
        res = os.path.join(cfg["state_dir"], "camera-status")
        try:
            os.unlink(res)
        except OSError:
            pass
        subprocess.run(["open", "-W", "-g", "-n", "-a", app, "--args", "--camera-status-file", res],
                       capture_output=True, timeout=20)
        try:
            auth = open(res).read().strip()
        except OSError:
            auth = "unknown"
        # Advisory only: on Eugene's Mac (macOS 27) frames flow with RouteC still 'notDetermined'.
        # The picture row below is what decides; this row says what to do if that ever changes.
        row("OK" if auth == "authorized" else "WARN", "camera grant",
            auth if auth == "authorized" else auth + " (only matters if the picture check fails: then System "
            "Settings > Privacy & Security > Camera > turn on RouteC, and `routec restart`)")
    else:
        row("WARN", "camera grant", "RouteC.app not installed (run install.sh); the daemon then needs a "
                                    "camera-capable parent shell (`routec start --here`)")
    if os.path.exists(agent_plist()):
        row("OK" if agent_loaded() else "WARN", "launchd agent", "loaded" if agent_loaded() else
            "installed but not loaded: `routec start`")
    else:
        row("WARN", "launchd agent", "not installed (run install.sh); daemon will not survive a reboot")
    if not c.ping():
        row("..", "daemon", "not running; starting it")
        if not start(cfg, quiet=True):
            row("FAIL", "daemon", "would not start. See `routec logs`.")
            return 1
    s, st = c.call("POST", "/status", {"deep": True}, timeout=60)
    if s != 200:
        row("FAIL", "daemon", "status failed: %s" % st)
        return 1
    row("OK", "daemon", "routecd %s, up %.0fs" % (st.get("version"), st.get("uptime", 0)))
    cap = st["capture"]
    good = st["verdict"].startswith("OK")
    row("OK" if good else "FAIL", "picture", st["verdict"])
    if cap.get("stderr_tail") and not good:
        row("..", "ffmpeg said", " | ".join(cap["stderr_tail"][-3:]))
    lk = st.get("locked") or {}
    if lk.get("locked"):
        row("FAIL", "laptop", "LOCKED / SIGN-IN: %s. Never type a password: Eugene must unlock it." % lk.get("why"))
    elif "locked" in lk:
        row("OK", "laptop", "not locked, no password prompt visible")
    ports = [p for p in os.listdir("/dev") if p.startswith("cu.usbserial-")] if os.path.isdir("/dev") else []
    row("OK" if ports else "FAIL", "kmbox port", ", ".join("/dev/" + p for p in ports) if ports else
        "no /dev/cu.usbserial-*: the KMBox USB(COM) cable is unplugged or in the USB(PC) socket")
    km = st["kmbox"]
    if km.get("scale"):
        row("OK", "pointer scale", "%.3f (%s)" % (km["scale"], km.get("scale_how")))
    ocr = st["ocr"]
    row("OK" if ocr["engine"] == "vision" else "WARN", "ocr", ocr["engine"] + (
        "" if ocr["engine"] == "vision" else " - reads are DEGRADED: %s" % ocr.get("vision_error")))
    row("OK", "lease", st["lease"])
    row("OK" if not st["presence"]["active"] else "WARN", "presence", st["presence"]["verdict"])
    return 0 if all(r[0] != "FAIL" for r in rows) else 1


# ── main ────────────────────────────────────────────────────────────────────────────────────
def main(argv):
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    verb, a = argv[0], Args(argv[1:])
    cfg = config_mod.load()
    c = Client(cfg)
    J = bool(a.get("--json"))

    if verb == "daemon":
        from .server import serve
        return serve(cfg)
    if verb == "start":
        return 0 if start(cfg, here=bool(a.get("--here"))) else 4
    if verb == "stop":
        return 0 if stop(cfg) else 1
    if verb == "restart":
        stop(cfg)
        time.sleep(1)
        return 0 if start(cfg, here=bool(a.get("--here"))) else 4
    if verb == "logs":
        n = int(a.pos[0]) if a.pos else 40
        path = os.path.join(cfg["log_dir"], "routecd.log")
        try:
            print("".join(open(path).readlines()[-n:]))
        except OSError:
            print("no log at %s" % path)
        return 0
    if verb == "doctor":
        return doctor(cfg, c)

    try:
        if not c.ping() and not start(cfg, quiet=True):
            print("routec: the Route C daemon is not running and would not start. Run `routec doctor`.",
                  file=sys.stderr)
            return 4
        return dispatch(verb, a, c, cfg, J)
    except Unreachable as e:
        print("routec: lost the daemon mid-call (%s). If this was an input command, check `routec jobs` "
              "before doing anything else." % e, file=sys.stderr)
        return 4


def dispatch(verb, a, c, cfg, J):
    fresh = bool(a.get("--fresh"))
    box = ints(a.get("--box"), 4, "--box") if a.get("--box") else None

    if verb == "status":
        s, b = c.call("POST", "/status", {"deep": bool(a.get("--deep"))}, timeout=60)
        if s != 200:
            return fail(s, b)
        if J:
            out(b, True)
        else:
            print(b["verdict"])
            print("presence: %s | lease: %s | ocr: %s | jobs busy: %s" % (
                b["presence"]["verdict"], b["lease"], b["ocr"]["engine"], b["jobs_busy"]))
            if b.get("locked", {}).get("locked"):
                print("LOCKED: %s" % b["locked"]["why"])
        return 0 if b["verdict"].startswith("OK") else 5

    if verb == "screen":
        bx = ints(a.pos, 4, "screen box") if a.pos else box
        s, b = c.call("POST", "/screen", {"box": bx, "fresh": fresh})
        if s != 200:
            return fail(s, b)
        warn_frame(b)
        if J:
            out(b, True)
        else:
            for l in b["lines"]:
                print("%5d %5d  %s" % (l["cx"], l["cy"], l["text"]))
            print("# %d lines | frame %d, %.1fs old, %s | ocr %s" % (
                len(b["lines"]), b["frame"]["seq"], b["frame"]["age"], b["frame"]["health"]["state"], b["engine"]),
                file=sys.stderr)
        return 0

    if verb == "find":
        if not a.pos:
            return usage("find TEXT")
        s, b = c.call("POST", "/find", {"text": " ".join(a.pos), "box": box, "fresh": fresh})
        if s != 200:
            return fail(s, b)
        warn_frame(b)
        out(b if J else json.dumps(b["hits"]))
        return 0 if b["hits"] else 1

    if verb == "ocr":
        s, b = c.call("POST", "/ocr", {"box": ints(a.pos, 4, "ocr box"), "fresh": fresh})
        if s != 200:
            return fail(s, b)
        warn_frame(b)
        out(b if J else b["text"])
        return 0

    if verb == "look":
        body = {"box": ints(a.pos, 4, "look box") if a.pos else box, "width": int(a.get("--width", 1600)),
                "crops": [ints(cr, 4, "--crop") for cr in a.get("--crop", [])], "fmt": "png" if a.get("--png") else "jpg"}
        s, b = c.call("POST", "/look", body)
        if s != 200:
            return fail(s, b)
        warn_frame(b)
        out(b if J else "\n".join(b["paths"]))
        return 0

    if verb == "grab":
        if not a.pos:
            return usage("grab OUT.png [X Y W H]")
        s, b = c.call("POST", "/grab", {"path": a.pos[0], "box": ints(a.pos[1:5], 4, "grab box") if len(a.pos) > 1 else None})
        if s != 200:
            return fail(s, b)
        warn_frame(b)
        out(b if J else "ok %s %dx%d" % (b["path"], b["size"][0], b["size"][1]))
        return 0

    if verb == "wait":
        t = float(a.get("--timeout", 15))
        body = {"text": " ".join(a.pos), "box": box, "timeout": t, "gone": bool(a.get("--gone")),
                "change": bool(a.get("--change"))}
        s, b = c.call("POST", "/wait", body, timeout=t + 30)
        if s != 200:
            return fail(s, b)
        out(b)
        return 0 if b.get("ok") else 1

    if verb == "presence":
        s, b = c.call("GET", "/presence")
        out(b)
        return 0 if not b.get("active") else 1

    if verb == "view":
        url = "http://127.0.0.1:%d/view?t=%s" % (cfg["port"], c.token())
        print(url)
        if a.get("--open"):
            subprocess.run(["open", url])
        return 0

    if verb == "lease":
        if not a.pos:
            return usage("lease OWNER [MINUTES] [--unattended]")
        s, b = c.call("POST", "/lease", {"owner": a.pos[0], "minutes": float(a.pos[1]) if len(a.pos) > 1 else 15,
                                         "unattended": bool(a.get("--unattended"))})
        out(b if J or s != 200 else "lease held by %s until %s%s" % (
            b["lease"]["owner"], b["lease"]["until_hm"], (" (%s)" % b["lease"]["note"]) if b["lease"].get("note") else ""))
        return 0 if s == 200 else 1

    if verb == "release":
        if not a.pos:
            return usage("release OWNER")
        s, b = c.call("POST", "/release", {"owner": a.pos[0]})
        out(b if J or s != 200 else "released")
        return 0 if s == 200 else 1

    if verb in ("click", "rclick", "dclick"):
        p = common_params(a, click_params(a))
        kind = "rclick" if (verb == "rclick" or a.get("--right")) else ("dclick" if (verb == "dclick" or a.get("--double")) else "click")
        return submit(c, a, kind, p)
    if verb == "click-text":
        if not a.pos:
            return usage("click-text TEXT")
        p = {"text": " ".join(a.pos), "box": box}
        if a.get("--index") is not None:
            p["index"] = int(a.get("--index"))
        if a.get("--offset"):
            p["offset"] = ints(a.get("--offset"), 2, "--offset")
        return submit(c, a, "click_text", common_params(a, p))
    if verb in ("hover", "move"):
        x, y = ints(a.pos[:2], 2, "hover")
        return submit(c, a, "move", common_params(a, {"x": x, "y": y}))
    if verb == "type":
        text = " ".join(a.pos)
        if a.get("--stdin"):
            text = sys.stdin.read()
        if not text:
            return usage("type TEXT (or --stdin)")
        p = {"text": text}
        if a.get("--not-a-password"):
            p["not_a_password"] = True
        return submit(c, a, "type", common_params(a, p))
    if verb == "key":
        if not a.pos:
            return usage("key KEY [KEY...]")
        p = {"keys": a.pos}
        if a.get("--allow-bare"):
            p["allow_bare"] = True
        return submit(c, a, "key", common_params(a, p))
    if verb == "wheel":
        x, y, n = ints(a.pos[:3], 3, "wheel")
        return submit(c, a, "wheel", {"x": x, "y": y, "n": n, "repeat": int(a.get("--repeat", 1))})
    if verb == "run":
        src = a.pos[0] if a.pos else "-"
        raw = sys.stdin.read() if src == "-" else open(src).read()
        steps = json.loads(raw)
        p = steps if isinstance(steps, dict) else {"steps": steps}
        return submit(c, a, "batch", common_params(a, p))
    if verb == "wake":
        return submit(c, a, "wake", {})
    if verb == "calibrate":
        return submit(c, a, "calibrate", common_params(a, {}))

    if verb == "job":
        if not a.pos:
            return usage("job ID [--wait S]")
        w = float(a.get("--wait", 0))
        s, b = c.call("GET", "/jobs/%s?wait=%s" % (a.pos[0], w), timeout=w + 15)
        if s != 200:
            return fail(s, b)
        return report_job(b["job"], a)
    if verb == "jobs":
        s, b = c.call("GET", "/jobs?limit=%d" % int(a.pos[0] if a.pos else 15))
        if J:
            out(b, True)
        else:
            for j in b["jobs"]:
                print("%s  %-12s %-10s %-11s %s" % (time.strftime("%H:%M:%S", time.localtime(j["created"])), j["id"],
                                                    j["kind"], j["status"], (j.get("error") or "")[:90]))
        return 0
    if verb == "cancel":
        s, b = c.call("POST", "/jobs/%s/cancel" % a.pos[0])
        out(b)
        return 0 if s == 200 else 1
    if verb == "restart-capture":
        s, b = c.call("POST", "/capture/restart")
        out(b)
        return 0
    return usage("unknown command %r" % verb)
