"""The capture stream: one long-lived ffmpeg reading the Elgato, latest frame always in memory.

Why this replaces a fresh ffmpeg per read (kvm_native.Capture.frame):
  * Camera permission (TCC) belongs to the process that opens the device. A per-call ffmpeg
    inherits it from whichever shell ran it, so the same command works from one Desktop
    Commander and hangs from another (30 Oct, 2 Nov 2026). Here only the daemon opens the device;
    callers talk HTTP and need no camera permission at all.
  * The first frames after opening a UVC device can be black. A one-shot grab takes frame one.
    Here the stream is already warm; frames inside warmup_seconds of an open are never served.
  * A stalled pipeline used to look like a blank or stale screen. Here a watchdog kills and
    reopens a stalled stream, and a read never returns a frame older than the stream allows: it
    waits for a fresh one or fails with a named cause.
"""
import collections
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .frames import Frame, classify


class CaptureError(RuntimeError):
    def __init__(self, code, message, hint=None):
        super().__init__(message)
        self.code = code
        self.hint = hint

    def as_dict(self):
        return {"code": self.code, "message": str(self), "hint": self.hint}


HINTS = {
    "device_missing": "The Mac cannot see the Elgato 4K X. Check its USB-C cable to the Mac and that "
                      "it has power; `ffmpeg -f avfoundation -list_devices true -i \"\"` should list it.",
    "device_busy": "Another app has locked the Elgato (usually OBS). Quit OBS, or leave it running with "
                   "its WebSocket server on so Route C can read frames through it.",
    "format_unsupported": "The Elgato is not offering %dx%d. The laptop's display mode has changed: "
                          "restore the Route C display mode on the laptop (Settings > System > Display).",
    "no_frames": "ffmpeg opened the Elgato but no frame arrived. Usual causes, in order: (1) the process "
                 "that started the Route C daemon has no Camera permission (macOS then blocks the open "
                 "silently): run `routec doctor`; (2) no HDMI signal from the laptop (lid closed, cable).",
    "stalled": "Frames stopped arriving mid-stream; the stream was reopened automatically.",
    "ffmpeg_missing": "ffmpeg is not installed at the configured path (brew install ffmpeg).",
    "ffmpeg_exit": "ffmpeg exited unexpectedly; see stderr_tail.",
    "stopped": "The capture stream is stopped (idle). It restarts on the next read.",
}


def classify_stderr(lines):
    text = "\n".join(lines).lower()
    if "could not lock" in text or "input/output error" in text or "device busy" in text:
        return "device_busy"
    if "selected video size" in text or "not supported by the device" in text or "supported modes" in text:
        return "format_unsupported"
    if "video device not found" in text or "no such" in text or "not found" in text:
        return "device_missing"
    return None


class FrameStream:
    def __init__(self, cfg, log=None, on_frame=None):
        self.cfg = cfg
        self.log = log or (lambda *a: None)
        self.on_frame = on_frame
        self.size = tuple(cfg["video_size"])
        self.frame_bytes = self.size[0] * self.size[1] * 3
        self._cond = threading.Condition()
        self._latest = None
        self._seq = 0
        self._proc = None
        self._proc_started = 0.0
        self._last_frame_at = 0.0
        self._frames_this_proc = 0
        self._stderr = collections.deque(maxlen=30)
        self._error = None
        self._restarts = 0
        self._stop = threading.Event()
        self._demand = threading.Event()
        self._last_client = time.time()
        self._times = collections.deque(maxlen=40)
        self._source = "ffmpeg"
        self._busy_strikes = 0
        self._thread = None
        self.camera_auth = os.environ.get("ROUTEC_CAMERA_AUTH", "unknown")

    # ── public ──────────────────────────────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="capture", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._demand.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=5)

    def restart(self):
        self.log("capture: restart requested")
        self._kill()

    def touch(self):
        self._last_client = time.time()
        self._demand.set()

    def latest(self):
        return self._latest

    def max_age(self):
        """Oldest a frame may be and still count as 'current'."""
        return max(1.5, 3.0 / float(self.cfg["stream_fps"]))

    def get(self, fresh_after=None, timeout=8.0):
        """The current frame. With fresh_after (a time.time() value), a frame captured after that
        moment: use it after an action so you see its effect, never the screen from before it."""
        self.touch()
        deadline = time.time() + timeout
        with self._cond:
            while True:
                now = time.time()
                f = self._latest
                if f is not None:
                    if fresh_after is not None:
                        if f.ts > fresh_after:
                            return f
                    elif now - f.ts <= self.max_age():
                        return f
                if now >= deadline:
                    raise self._no_frame_error(f)
                self._cond.wait(min(0.5, deadline - now))

    def status(self):
        now = time.time()
        f = self._latest
        fps = 0.0
        if len(self._times) >= 2 and now - self._times[-1] < 5:
            span = self._times[-1] - self._times[0]
            fps = (len(self._times) - 1) / span if span > 0 else 0.0
        if self._stop.is_set():
            state = "stopped"
        elif f is not None and now - f.ts <= self.max_age():
            state = "streaming"
        elif self._proc is not None and self._proc.poll() is None:
            state = "starting" if self._frames_this_proc == 0 else "stalled"
        elif self._idle():
            state = "idle"
        else:
            state = "error" if self._error else "starting"
        return {
            "state": state,
            "source": self._source,
            "pid": self._proc.pid if self._proc and self._proc.poll() is None else None,
            "fps": round(fps, 2),
            "last_frame_age": round(now - f.ts, 2) if f else None,
            "frame": f.meta() if f else None,
            "restarts": self._restarts,
            "error": self._error.as_dict() if self._error else None,
            "stderr_tail": list(self._stderr)[-8:],
            "camera_auth": self.camera_auth,
            "size": list(self.size),
        }

    # ── internals ───────────────────────────────────────────────────────────────────────────
    def _idle(self):
        n = self.cfg.get("idle_stop_seconds") or 0
        return n > 0 and time.time() - self._last_client > n

    def _no_frame_error(self, f):
        err = self._error
        if err is not None:
            return CaptureError(err.code, "no current frame: %s" % err, err.hint)
        if f is None:
            return CaptureError("no_frames", "no frame has arrived from the Elgato yet", HINTS["no_frames"])
        return CaptureError("stalled", "the newest frame is %.1fs old; the stream is not delivering"
                            % (time.time() - f.ts), HINTS["stalled"])

    def _cmd(self):
        w, h = self.size
        custom = self.cfg.get("capture_cmd")
        if custom:
            return [str(a).format(w=w, h=h, fps=self.cfg["stream_fps"]) for a in custom]
        return [self.cfg["ffmpeg"], "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-f", "avfoundation", "-framerate", str(self.cfg["framerate"]),
                "-video_size", "%dx%d" % (w, h), "-pixel_format", self.cfg["pixel_format"],
                "-i", self.cfg["device"], "-an", "-vf", "fps=%s" % self.cfg["stream_fps"],
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]

    def _set_error(self, code, message=None, extra=None):
        hint = HINTS.get(code)
        if code == "format_unsupported":
            hint = hint % self.size
        if code == "no_frames" and self.camera_auth not in ("unknown", "authorized"):
            hint = ("Camera permission for the Route C daemon is '%s'. Open System Settings > Privacy & "
                    "Security > Camera and switch RouteC on, then `routec restart`." % self.camera_auth)
        msg = message or code
        if extra:
            msg += " (%s)" % extra
        self._error = CaptureError(code, msg, hint)
        self.log("capture error: %s: %s" % (code, msg))

    def _kill(self):
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.kill()           # SIGKILL: stuck avfoundation ffmpegs ignore SIGTERM (30 Oct 2026)
                p.wait(timeout=3)
            except Exception:
                pass

    def _supervise(self):
        backoff = 1.0
        while not self._stop.is_set():
            if self._idle():
                self._kill()
                self._demand.clear()
                self._demand.wait(timeout=5)
                continue
            if self._source == "obs":
                if self._obs_loop(seconds=60):
                    continue
                self._source = "ffmpeg"
            ok = self._run_once()
            if self._stop.is_set():
                break
            if ok:
                backoff = 1.0
            else:
                if self._error and self._error.code == "device_busy":
                    self._busy_strikes += 1
                    if self._busy_strikes >= 2 and self.cfg.get("obs_fallback"):
                        self._source = "obs"
                        continue
                time.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    def _run_once(self):
        """Run one ffmpeg until it exits or stalls. True if it delivered frames."""
        cmd = self._cmd()
        self._stderr.clear()
        self._frames_this_proc = 0
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          stdin=subprocess.DEVNULL, bufsize=0)
        except FileNotFoundError:
            self._set_error("ffmpeg_missing", "cannot run %s" % cmd[0])
            return False
        self._proc_started = time.time()
        self._restarts += 1
        self.log("capture: started pid %d (%s)" % (self._proc.pid, " ".join(cmd[:3]) + " ..."))
        proc = self._proc
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
        reader = threading.Thread(target=self._read_frames, args=(proc,), daemon=True)
        reader.start()
        stall = float(self.cfg["stall_seconds"])
        while not self._stop.is_set():
            time.sleep(0.25)
            if proc.poll() is not None:
                reader.join(timeout=2)
                code = classify_stderr(self._stderr)
                if self._frames_this_proc == 0:
                    self._set_error(code or "ffmpeg_exit", "ffmpeg exited with %s before any frame"
                                    % proc.returncode, (self._stderr[-1] if self._stderr else None))
                else:
                    self._set_error(code or "ffmpeg_exit", "ffmpeg exited with %s mid-stream" % proc.returncode)
                return self._frames_this_proc > 0
            if self._idle():
                self._kill()
                return True
            since = time.time() - max(self._proc_started, self._last_frame_at)
            if since > stall + (4 if self._frames_this_proc == 0 else 0):
                if self._frames_this_proc == 0:
                    code = classify_stderr(self._stderr) or "no_frames"
                    self._set_error(code, "no frame %.0fs after opening the device" % since)
                else:
                    self._set_error("stalled", "no frame for %.0fs mid-stream" % since)
                self._kill()
                return self._frames_this_proc > 0
        self._kill()
        return True

    def _read_stderr(self, proc):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr.append(line)

    def _read_frames(self, proc):
        n = self.frame_bytes
        out = proc.stdout
        while True:
            buf = bytearray(n)
            view = memoryview(buf)
            got = 0
            while got < n:
                k = out.readinto(view[got:])
                if not k:
                    return
                got += k
            self._deliver(bytes(buf), "ffmpeg")

    def _deliver(self, data, source, image=None):
        now = time.time()
        self._last_frame_at = now
        self._frames_this_proc += 1
        self._times.append(now)
        if now - self._proc_started < float(self.cfg["warmup_seconds"]) and source == "ffmpeg":
            return                                  # warm-up frames can be black: never serve them
        self._seq += 1
        f = Frame(self._seq, now, self.size, data, source, image=image)
        f.health = classify(f)
        if self._error is not None:
            self.log("capture: recovered (%s)" % self._error.code)
        self._error = None
        self._busy_strikes = 0
        if self.on_frame:
            try:
                self.on_frame(f)
            except Exception as e:              # a presence bug must never stop the stream
                self.log("on_frame error: %s" % e)
        with self._cond:
            self._latest = f
            self._cond.notify_all()

    def _obs_loop(self, seconds):
        """Read frames through OBS's WebSocket while OBS holds the device. True if it worked."""
        try:
            sys.path.insert(0, self.cfg["proxy_dir"])
            import obs_capture  # Eugene's Proxy/obs_capture.py
        except Exception as e:
            self._set_error("device_busy", "device locked and OBS fallback unavailable", str(e))
            return False
        from PIL import Image
        tmp = Path(self.cfg["state_dir"]) / "obs_frame.png"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        self.log("capture: device locked; reading frames through OBS")
        end = time.time() + seconds
        delivered = 0
        self._proc_started = 0.0
        while time.time() < end and not self._stop.is_set():
            t0 = time.time()
            try:
                obs_capture.screenshot(str(tmp), size=self.size)
                img = Image.open(tmp).convert("RGB")
                img.load()
                self._deliver(None, "obs", image=img)
                delivered += 1
            except Exception as e:
                self._set_error("device_busy", "OBS fallback failed", str(e)[:200])
                return delivered > 0
            time.sleep(max(0.0, 1.0 / float(self.cfg["stream_fps"]) - (time.time() - t0)))
        return delivered > 0


def verdict(status):
    """One plain-English line on whether Claude can see the laptop, and if not, why."""
    s = status.get("state")
    f = status.get("frame") or {}
    h = f.get("health") or {}
    if s == "streaming":
        hs = h.get("state")
        if hs == "live":
            return "OK: live %dx%d picture, %.1f fps, newest frame %.1fs old." % (
                tuple(f["size"]) + (status["fps"], status["last_frame_age"]))
        return "CAPTURE OK, LAPTOP PICTURE %s: %s." % ((hs or "?").upper(), h.get("detail"))
    err = status.get("error") or {}
    if s == "idle":
        return "IDLE: stream paused to save power; the next read restarts it (about 2s)."
    if s == "starting" and not err:
        return "STARTING: opening the Elgato."
    return "NO PICTURE (%s): %s. %s" % (err.get("code", s), err.get("message", s), err.get("hint") or "")
