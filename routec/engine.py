"""The engine: capture + OCR + KMBox + presence + lease, and the input job runner.

Input jobs pass a pre-flight before anything is sent:
  1. the caller holds the input lease;
  2. a current frame exists and the laptop picture is live (not black, not a flat colour, not a
     Windows lock/sign-in screen; letterboxed only when allowed explicitly);
  3. the screen has been quiet for presence_window seconds (nobody else is using it);
  4. for typing: no visible password prompt (hard limit: never type a password).
Each step re-checks presence, so a batch stops as soon as someone else starts using the laptop.
"""
import os
import threading
import time
from pathlib import Path

from . import lease as leases
from .capture import CaptureError, FrameStream
from .frames import diff_summary
from .kmbox import KMBox, KMBoxError, MaybeExecuted, Refused, Rejected, VALID_KEYS, FORBIDDEN_BARE
from .ocr import OCR, looks_locked
from .presence import HumanActive, Presence

PASSWORD_LINES = {"password", "enter password", "enter your password", "passcode", "pin", "enter pin",
                  "enter your pin", "current password", "new password", "confirm password", "password:"}


class InputError(RuntimeError):
    def __init__(self, code, message, refused=False, may_have_executed=None, partial=None):
        super().__init__(message)
        self.code = code
        self.refused = refused
        self.may_have_executed = may_have_executed
        self.partial = partial


class Cancelled(Exception):
    pass


def to_box(b):
    """[x, y, w, h] -> (x0, y0, x1, y1); None stays None."""
    if not b:
        return None
    x, y, w, h = (int(v) for v in b)
    return (x, y, x + w, y + h)


class Snapshots:
    def __init__(self, cfg):
        self.dir = Path(cfg["snapshot_dir"])
        self.keep = int(cfg.get("snapshot_keep", 60))

    def save(self, frame, box=None, width=None, label="look", fmt="jpg"):
        self.dir.mkdir(parents=True, exist_ok=True)
        img = frame.image()
        if box:
            img = img.crop(box)
        if width and img.width > width:
            img = img.resize((int(width), max(1, int(img.height * width / img.width))))
        name = "%s-%s-%d.%s" % (time.strftime("%Y%m%d-%H%M%S"), label, frame.seq, fmt)
        path = self.dir / name
        if fmt == "png":
            img.save(path)
        else:
            img.convert("RGB").save(path, quality=85)
        self.prune()
        return str(path)

    def prune(self):
        files = sorted(self.dir.glob("*.*"), key=lambda p: p.stat().st_mtime)
        for p in files[:-self.keep] if self.keep > 0 else []:
            try:
                p.unlink()
            except OSError:
                pass


class Engine:
    def __init__(self, cfg, log=None, km_factory=None, ocr_engine=None, start_capture=True):
        self.cfg = cfg
        self.log = log or (lambda *a: None)
        self.presence = Presence(cfg)
        self.capture = FrameStream(cfg, self.log, on_frame=self.presence.feed)
        self.ocr = OCR(cfg, self.log, engine=ocr_engine)
        self.km = KMBox(cfg, self.log, port_factory=km_factory)
        self.io = threading.RLock()            # one owner of the serial port at a time
        self.snaps = Snapshots(cfg)
        self.started = time.time()
        if start_capture:
            self.capture.start()
        threading.Thread(target=self._idle_closer, daemon=True).start()

    # ── reads ───────────────────────────────────────────────────────────────────────────────
    def frame(self, fresh=False, fresh_after=None, timeout=8.0):
        if fresh and fresh_after is None:
            fresh_after = time.time()
        return self.capture.get(fresh_after=fresh_after, timeout=timeout)

    def lines(self, box=None, frame=None):
        f = frame or self.frame()
        lines, engine = self.ocr.lines(f.image(), box)
        return f, lines, engine

    def find(self, text, box=None, frame=None):
        f = frame or self.frame()
        hits, engine = self.ocr.find(f.image(), text, box)
        return f, hits, engine

    def wait_text(self, text, box=None, timeout=15.0, gone=False, after=None):
        """Poll fresh frames until `text` appears (or, with gone, disappears)."""
        deadline = time.time() + float(timeout)
        last = after or time.time()
        tries = 0
        while True:
            f = self.capture.get(fresh_after=last, timeout=max(1.0, deadline - time.time() + 1))
            last = f.ts
            tries += 1
            hits, engine = self.ocr.find(f.image(), text, box)
            if bool(hits) != bool(gone):
                return {"ok": True, "hits": hits, "frame": f.meta(), "polls": tries, "engine": engine}
            if time.time() >= deadline:
                return {"ok": False, "hits": hits, "frame": f.meta(), "polls": tries, "engine": engine,
                        "error": "%r still %s after %.0fs" % (text, "present" if gone else "absent", timeout)}

    def wait_change(self, box=None, timeout=10.0, threshold=0.0005):
        start = self.frame()
        deadline = time.time() + float(timeout)
        last = start.ts
        while time.time() < deadline:
            f = self.capture.get(fresh_after=last, timeout=max(1.0, deadline - time.time() + 1))
            last = f.ts
            d = diff_summary(start, f)
            if d["bbox"] and d["changed"] >= threshold and _overlaps(d["bbox"], box):
                return {"ok": True, "change": d, "frame": f.meta()}
        return {"ok": False, "error": "no change in %.0fs" % timeout}

    def locked_check(self, frame):
        """(is_locked_or_password_prompt, reason). Full-frame OCR; ~1-2 s."""
        lines, _ = self.ocr.lines(frame.image())
        locked, words = looks_locked(lines)
        if locked:
            return True, "Windows lock / sign-in screen (%s)" % ", ".join(words)
        pw = [l["text"] for l in lines if l["text"].strip().lower().rstrip("*•· ") in PASSWORD_LINES]
        if pw:
            return True, "a password prompt is visible (%r)" % pw[0]
        return False, None

    # ── status ──────────────────────────────────────────────────────────────────────────────
    def status(self):
        """Current state. Asking for status is a reason to look: a paused stream is restarted and
        given a few seconds to deliver, so the verdict is about the laptop now, not 'idle'."""
        from .capture import verdict
        if self.capture.status()["state"] in ("idle", "starting"):
            try:
                self.capture.get(timeout=6)
            except CaptureError:
                pass
        else:
            self.capture.touch()
        cs = self.capture.status()
        return {
            "capture": cs,
            "verdict": verdict(cs),
            "presence": self.presence.report(),
            "lease": leases.describe(leases.state(self.cfg["lease_file"])),
            "kmbox": {"open": self.km.is_open(), "port": self.km.port_name, "version": self.km.version,
                      "scale": self.km.scale, "scale_how": self.km.scale_how},
            "ocr": self.ocr.status(),
            "uptime": round(time.time() - self.started, 1),
        }

    # ── input jobs ──────────────────────────────────────────────────────────────────────────
    def run_job(self, job, cancel):
        kind, p, owner = job["kind"], job["params"] or {}, job["owner"]
        progress = lambda v: job.__setitem__("progress", v)
        with self.io:
            try:
                return self._run(kind, p, owner, cancel, progress)
            except InputError:
                raise
            except leases.LeaseError as e:
                raise InputError("lease", str(e), refused=True, may_have_executed=False)
            except HumanActive as e:
                raise InputError("human_active", str(e), refused=True, may_have_executed=False)
            except CaptureError as e:
                raise InputError("capture_" + e.code, "%s. %s" % (e, e.hint or ""), refused=True,
                                 may_have_executed=False)
            except Refused as e:
                raise InputError("refused", str(e), refused=True, may_have_executed=False)
            except Rejected as e:
                raise InputError("rejected", str(e), may_have_executed=False)
            except MaybeExecuted as e:
                raise InputError("maybe_executed", str(e) + ". Read the screen before doing anything else; "
                                 "do not resend.", may_have_executed=True)
            except Cancelled as e:
                raise InputError("cancelled", "cancelled: %s" % e, may_have_executed=True)
            except KMBoxError as e:
                raise InputError("kmbox", str(e), may_have_executed=False)

    def _preflight(self, owner, p, need_live=True):
        leases.require(self.cfg["lease_file"], owner)
        f = self.frame(fresh=True)
        h = f.health or {}
        state = h.get("state")
        if need_live and state in ("black", "uniform"):
            raise InputError("screen_" + state, "not sending input: %s. If the laptop is asleep, run `routec wake` "
                             "first." % h.get("detail"), refused=True, may_have_executed=False)
        if state == "letterboxed":
            if not p.get("allow_letterbox"):
                raise InputError("letterboxed", "not sending input: %s. Restore the Route C display mode on "
                                 "the laptop first (pass allow_letterbox to drive Settings to do it)."
                                 % h.get("detail"), refused=True, may_have_executed=False)
            self.km.origin = tuple(h["content_box"][:2])
        else:
            self.km.origin = (0, 0)
        if not p.get("skip_presence"):
            self.presence.require_quiet()
        return f

    def _get_frame_for_km(self, after):
        return self.capture.get(fresh_after=after, timeout=6)

    def _scale(self):
        with self.io:
            self.km.open()
            return self.km.ensure_scale(self._get_frame_for_km)

    def _run(self, kind, p, owner, cancel, progress):
        if kind == "batch":
            return self._batch(p, owner, cancel, progress)
        if kind == "wake":
            return self._wake(owner)
        f0 = self._preflight(owner, p)
        if kind == "calibrate":
            self.km.open()
            self.presence.own_input(10)
            s, how = self.km.calibrate(self._get_frame_for_km)
            self.presence.own_input()
            return {"scale": round(s, 4), "how": how}
        step = dict(p)
        step["do"] = kind
        return self._step(step, f0, cancel, progress)

    def _check_type_safe(self, p):
        f = self.frame(fresh=True)
        bad, why = self.locked_check(f)
        if bad and not (p.get("not_a_password") and "lock" not in why):
            raise InputError("password_guard", "not typing: %s. Hard limit: never type a password. If the "
                             "lapsed session needs a sign-in, stop and tell Eugene." % why, refused=True,
                             may_have_executed=False)

    def _step(self, s, before, cancel, progress):
        """Execute one input step. `before` is a frame from before the step (for change detection)."""
        do = s["do"]
        if cancel.is_set():
            raise Cancelled("before step %s" % do)
        if do in ("click", "rclick", "dclick", "move", "hover", "wheel", "click_text"):
            self._scale()
        if do == "type":
            self._check_type_safe(s)
        self.km.open()
        self.presence.own_input()
        t0 = time.time()
        out = {}
        try:
            if do in ("click", "rclick", "dclick"):
                button = "right" if do == "rclick" else s.get("button", "left")
                count = 2 if do == "dclick" else int(s.get("count", 1))
                mods = list(s.get("mods", []))
                bad = [m for m in mods if m not in VALID_KEYS]
                if bad:
                    raise Refused("unknown modifier %s; nothing was sent" % bad)
                self.km.click(int(s["x"]), int(s["y"]), button, mods, count)
                out.update(x=int(s["x"]), y=int(s["y"]), button=button, count=count, mods=mods)
            elif do in ("move", "hover"):
                self.km.moveto(int(s["x"]), int(s["y"]))
                out.update(x=int(s["x"]), y=int(s["y"]))
            elif do == "type":
                def prog(n):
                    progress({"typed": n, "of": len(s["text"])})
                    self.presence.own_input()
                    if cancel.is_set():
                        raise Cancelled("after %d of %d characters" % (n, len(s["text"])))
                n = self.km.type(s["text"], progress=prog)
                out.update(typed=n)
            elif do == "key":
                keys = s.get("keys") or [s["key"]]
                self.km.key(*keys, allow_bare=bool(s.get("allow_bare")))
                out.update(keys=keys)
            elif do == "wheel":
                self.km.moveto(int(s["x"]), int(s["y"]))
                n = int(s["n"])
                reps = int(s.get("repeat", 1))
                for _ in range(max(1, reps)):
                    self.km.wheel(n)
                out.update(n=n, repeat=reps)
            elif do == "click_text":
                f = self.frame(fresh=True)
                hits, _ = self.ocr.find(f.image(), s["text"], to_box(s.get("box")))
                idx = s.get("index")
                if not hits:
                    raise Refused("%r is not on screen; nothing was clicked" % s["text"])
                if len(hits) > 1 and idx is None:
                    raise Refused("%r matches %d places %s; pass index to choose. Nothing was clicked."
                                  % (s["text"], len(hits), [(h["cx"], h["cy"]) for h in hits[:6]]))
                h = hits[int(idx or 0)]
                dx, dy = s.get("offset", [0, 0])
                self.km.click(h["cx"] + int(dx), h["cy"] + int(dy), s.get("button", "left"),
                              list(s.get("mods", [])), int(s.get("count", 1)))
                out.update(x=h["cx"] + int(dx), y=h["cy"] + int(dy), matched=h["text"])
        finally:
            self.presence.own_input()
        if do in ("click", "rclick", "dclick", "click_text", "key", "type") and s.get("verify", True):
            after = self.capture.get(fresh_after=time.time() + 0.35, timeout=6)
            d = diff_summary(before, after) if before is not None else None
            out["screen_change"] = d and {"changed": d["changed"], "bbox": d["bbox"]}
            if d is not None and not d["bbox"]:
                out["warning"] = ("no visible change after this %s. Check the target on a fresh read "
                                  "before trying again; do not just repeat it." % do)
        out["seconds"] = round(time.time() - t0, 2)
        return out

    def _wake(self, owner):
        leases.require(self.cfg["lease_file"], owner)
        before = self.frame(fresh=True)
        self.km.open()
        self.presence.own_input(8)
        self.km.nudge()
        deadline = time.time() + 8
        f = before
        while time.time() < deadline:
            f = self.capture.get(fresh_after=f.ts, timeout=6)
            if (f.health or {}).get("state") == "live":
                break
        return {"before": (before.health or {}).get("state"), "after": (f.health or {}).get("state"),
                "detail": (f.health or {}).get("detail")}

    # ── batches ─────────────────────────────────────────────────────────────────────────────
    INPUT_STEPS = {"click", "rclick", "dclick", "move", "hover", "type", "key", "wheel", "click_text"}
    READ_STEPS = {"sleep", "screen", "ocr", "find", "assert", "refute", "wait", "wait_change", "snapshot",
                  "presence"}

    @classmethod
    def validate_steps(cls, steps):
        """Reject a bad batch before anything is sent. Returns None or an error string."""
        if not isinstance(steps, list) or not steps:
            return "steps must be a non-empty list"
        for i, s in enumerate(steps):
            do = s.get("do") if isinstance(s, dict) else None
            if do not in cls.INPUT_STEPS | cls.READ_STEPS:
                return "step %d: unknown step %r" % (i, do)
            need = {"click": "xy", "rclick": "xy", "dclick": "xy", "move": "xy", "hover": "xy", "wheel": "xyn"}
            for k in need.get(do, ""):
                if k not in s:
                    return "step %d (%s): missing %r" % (i, do, k)
            if do in ("type",) and not isinstance(s.get("text"), str):
                return "step %d: type needs text" % i
            if do in ("find", "assert", "refute", "wait", "click_text") and not s.get("text"):
                return "step %d (%s): needs text" % (i, do)
            if do == "key":
                keys = s.get("keys") or ([s["key"]] if "key" in s else [])
                if not keys:
                    return "step %d: key step has no keys" % i
                bad = [k for k in keys if str(k).lower() not in VALID_KEYS]
                if bad:
                    return "step %d: unknown key(s) %s" % (i, bad)
                if len(keys) == 1 and str(keys[0]).lower() in FORBIDDEN_BARE and not s.get("allow_bare"):
                    return "step %d: refusing bare %r (Laurel Release/Delete)" % (i, keys[0])
            if do == "type":
                bad = KMBox.untypable(s["text"])
                if bad:
                    return "step %d: cannot type %r (ASCII only)" % (i, "".join(sorted(set(bad))))
        return None

    def _batch(self, p, owner, cancel, progress):
        steps = p.get("steps")
        err = self.validate_steps(steps)
        if err:
            raise InputError("bad_batch", err + ". Nothing was sent.", refused=True, may_have_executed=False)
        has_input = any(s["do"] in self.INPUT_STEPS for s in steps)
        before = self._preflight(owner, p) if has_input else self.frame()
        out = []
        for i, s in enumerate(steps):
            progress({"step": i, "of": len(steps), "do": s["do"]})
            rec = {"step": i, "do": s["do"]}
            try:
                if cancel.is_set():
                    raise Cancelled("before step %d" % i)
                if s["do"] in self.INPUT_STEPS:
                    if not p.get("skip_presence"):
                        self.presence.require_quiet(wait_history=False)
                    before = self.frame()
                    rec.update(self._step(s, before, cancel, progress))
                else:
                    ok, data = self._read_step(s)
                    rec.update(data)
                    if not ok:
                        rec["ok"] = False
                        out.append(rec)
                        raise InputError("step_failed", "step %d (%s) failed: %s" % (i, s["do"], data.get("error")),
                                         may_have_executed=False, partial={"stopped_at": i, "steps": out})
                rec["ok"] = True
                out.append(rec)
                if s.get("settle"):
                    time.sleep(float(s["settle"]))
            except InputError:
                raise
            except Exception as e:
                rec["ok"] = False
                rec["error"] = "%s: %s" % (type(e).__name__, e)
                out.append(rec)
                maybe = isinstance(e, (MaybeExecuted, Cancelled)) or (
                    s["do"] in self.INPUT_STEPS and not isinstance(e, (Refused, Rejected, HumanActive, leases.LeaseError)))
                code = {HumanActive: "human_active", Refused: "refused", MaybeExecuted: "maybe_executed",
                        Cancelled: "cancelled"}.get(type(e), "step_failed")
                raise InputError(code, "stopped at step %d (%s): %s. Steps 0-%d ran." % (
                    i, s["do"], e, i - 1) if i else "stopped at step 0 (%s): %s" % (s["do"], e),
                    may_have_executed=maybe, partial={"stopped_at": i, "steps": out})
        return {"ok": True, "steps": out}

    def _read_step(self, s):
        do = s["do"]
        box = to_box(s.get("box"))
        if do in ("sleep", "wait", "wait_change"):
            # A batch waits for the effect of its own input (a page load, a dialog): changes during
            # the wait are ours, not a person's, so they must not trip the next presence check.
            self.presence.own_input(float(s.get("seconds", s.get("timeout", 15))))
            try:
                return self._wait_step(s, box)
            finally:
                self.presence.own_input()
        return self._plain_read_step(s, box)

    def _wait_step(self, s, box):
        do = s["do"]
        if do == "sleep":
            time.sleep(float(s.get("seconds", 1)))
            return True, {}
        if do == "wait":
            r = self.wait_text(s["text"], box, s.get("timeout", 15), bool(s.get("gone")))
            return r["ok"], {"hits": [(h["cx"], h["cy"], h["text"]) for h in r["hits"]][:5],
                             "error": r.get("error")}
        r = self.wait_change(box, s.get("timeout", 10))
        return r["ok"], {"change": r.get("change"), "error": r.get("error")}

    def _plain_read_step(self, s, box):
        do = s["do"]
        if do == "presence":
            r = self.presence.report(s.get("window"))
            return not r["active"], {"presence": r, "error": r["verdict"] if r["active"] else None}
        if do == "snapshot":
            f = self.frame(fresh=True)
            return True, {"path": self.snaps.save(f, box, s.get("width", 1600), s.get("label", "batch"))}
        f = self.frame(fresh=True)
        if do == "screen":
            _, lines, engine = self.lines(box, f)
            return True, {"lines": ["%5d %5d  %s" % (l["x"] + l["w"] // 2, l["y"] + l["h"] // 2, l["text"])
                                    for l in lines], "engine": engine}
        if do == "ocr":
            text, engine = self.ocr.text(f.image(), box)
            return True, {"text": text, "engine": engine}
        _, hits, engine = self.find(s["text"], box, f)
        if do == "find":
            return True, {"hits": [{"cx": h["cx"], "cy": h["cy"], "text": h["text"]} for h in hits], "engine": engine}
        want = do == "assert"
        ok = bool(hits) == want
        return ok, {"error": None if ok else "%r is %s on screen" % (s["text"], "absent" if want else "present"),
                    "engine": engine}

    # ── housekeeping ────────────────────────────────────────────────────────────────────────
    def _idle_closer(self):
        idle = float(self.cfg.get("serial_idle_close", 20))
        while True:
            time.sleep(2)
            if self.km.is_open() and time.time() - self.km.last_used > idle:
                if self.io.acquire(blocking=False):
                    try:
                        if time.time() - self.km.last_used > idle:
                            self.km.close()
                            self.log("kmbox: closed after %.0fs idle" % idle)
                    finally:
                        self.io.release()


def _overlaps(bbox, box):
    if not box:
        return True
    return not (bbox[2] < box[0] or bbox[0] > box[2] or bbox[3] < box[1] or bbox[1] > box[3])
