"""Presence: is someone (Eugene, or another agent) using the laptop right now?

The daemon sees every frame, so presence is watched continuously instead of sampled for six
seconds on demand. Ignored, as before: cursor-sized changes (the hardware jiggler, a caret
blink), the taskbar clock, black frames, and changes within own_input_grace of our own input.
Anything else is screen activity; input is refused until the screen has been quiet for
presence_window seconds.
"""
import collections
import threading
import time

from .frames import diff_summary


class HumanActive(RuntimeError):
    pass


class Presence:
    def __init__(self, cfg, clock=time.time):
        self.cfg = cfg
        self.clock = clock
        self._lock = threading.Lock()
        self._prev = None
        self._since = None
        self._own_until = 0.0
        self._last_activity = None
        self.events = collections.deque(maxlen=40)

    def own_input(self, extra=0.0):
        """Mark that we just sent input: changes in the next few seconds are ours."""
        with self._lock:
            self._own_until = max(self._own_until, self.clock() + float(self.cfg["own_input_grace"]) + extra)

    def reset(self):
        with self._lock:
            self._prev = None
            self._since = None

    def feed(self, frame):
        if (frame.health or {}).get("state") in ("black", "uniform"):
            return
        with self._lock:
            prev = self._prev
            self._prev = frame
            if prev is None or prev.size != frame.size:
                self._since = frame.ts
                return
            if frame.ts - prev.ts > 10:            # a gap in the stream: start watching afresh
                self._since = frame.ts
        d = diff_summary(prev, frame)
        sb = d.get("small_bbox")
        if not sb:
            return
        w, h = sb[2] - sb[0], sb[3] - sb[1]
        if w <= 30 and h <= 40:                    # cursor / jiggler / caret
            return
        if sb[1] >= 505 and sb[0] >= 840:          # taskbar clock and tray, bottom right
            return
        with self._lock:
            ours = frame.ts <= self._own_until
            ev = {"ts": round(frame.ts, 2), "changed": d["changed"], "bbox": d["bbox"], "ours": ours}
            self.events.append(ev)
            if not ours:
                self._last_activity = ev

    def report(self, window=None):
        window = float(window or self.cfg["presence_window"])
        now = self.clock()
        with self._lock:
            since, last = self._since, self._last_activity
        watched = (now - since) if since else 0.0
        age = (now - last["ts"]) if last else None
        active = age is not None and age < window
        if active:
            verdict = "ACTIVE: the screen changed %.0fs ago at %s, not from our input" % (age, last["bbox"])
        elif watched < window:
            verdict = "UNKNOWN: only %.0fs of history (need %.0fs)" % (watched, window)
        else:
            verdict = "quiet for %s" % ("%.0fs" % age if age is not None else "all %.0fs watched" % watched)
        return {"active": active, "verdict": verdict, "window": window, "watched_for": round(watched, 1),
                "last_activity_age": round(age, 1) if age is not None else None,
                "last_activity": last}

    def require_quiet(self, window=None, wait_history=True):
        """Raise HumanActive unless the screen has been quiet for `window` seconds. If the stream is
        too young to know, wait (up to `window`) for enough history first."""
        window = float(window or self.cfg["presence_window"])
        r = self.report(window)
        if not r["active"] and r["watched_for"] < window and wait_history:
            deadline = self.clock() + window - r["watched_for"] + 1
            while self.clock() < deadline:
                time.sleep(0.5)
                r = self.report(window)
                if r["active"] or r["watched_for"] >= window:
                    break
        if r["active"]:
            raise HumanActive("someone appears to be using the laptop (%s). Not sending input; try later."
                              % r["verdict"])
        if r["watched_for"] < window:
            raise HumanActive("cannot confirm the laptop is idle: %s" % r["verdict"])
        return r
