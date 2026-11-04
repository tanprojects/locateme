"""KMBox serial session: mouse and keyboard into the laptop.

The firmware handling is carried over unchanged from Proxy/kvm_native.py, where each rule was
paid for (dates are the route doc's):
  * MicroPython REPL at 115200 over pyserial; Ctrl-C first; `import km` explicitly (18 Sep).
  * km.mode(1) = USB mode, a fixed mouse+keyboard composite. Mode 0 silently kills the keyboard
    when no physical keyboard is attached to the box (21 Sep).
  * km.string() drops shift under dense runs, so only no-shift runs go through it; capitals and
    shifted symbols go per-char through km.multipress('shift-l', base) (19 Sep).
  * press()/multipress() cannot emit backtick; a lone ` or ~ goes through km.string (19 Sep).
  * The first HID event of a session is dropped: an invisible shift-l press absorbs it (19 Sep).
  * 0.09 s per key, 0.12 s per chord: faster drops ~3% of keystrokes (19 Sep).
  * Bare 'r' / 'del' via the key verb are refused: in Laurel they Release / Delete (17 Sep).
  * A lone Win key needs down / hold / up, not press (22 Sep).
  * The pointer is relative: park at (0,0) by edge-clamping, then move by a measured scale.

New here: every command waits for the REPL prompt with a hard timeout. A missing prompt raises
MaybeExecuted, never a silent retry, because on this route an error does not mean it did not happen.
"""
import glob
import json
import os
import time

SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
           "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";",
           '"': "'", "<": ",", ">": ".", "?": "/", "~": "`"}
PLAIN = set("abcdefghijklmnopqrstuvwxyz0123456789'-,./;=[]\\`")
NAMED = {" ": "space", "\n": "enter", "\t": "tab"}
STRINGABLE = (PLAIN - {"`"}) | {" "}
FORBIDDEN_BARE = {"r", "del", "delete"}
BUTTON_NUM = {"left": 0, "right": 1, "middle": 2}
VALID_KEYS = set("abcdefghijklmnopqrstuvwxyz0123456789'-,./;=[]\\`") | {
    "space", "enter", "tab", "back", "del", "esc", "caps", "insert", "home", "end", "pgup", "pgdown",
    "up", "down", "left", "right", "shift-l", "shift-r", "ctr-l", "ctr-r", "alt-l", "alt-r", "gui-l",
    "gui-r", "print", "scroll", "pause"} | {"f%d" % i for i in range(1, 21)}
PROMPT = b">>> "


class KMBoxError(RuntimeError):
    pass


class Rejected(KMBoxError):
    """The box answered with an error: the command did not run."""


class MaybeExecuted(KMBoxError):
    """No answer in time. The command was sent and may well have run. Read the screen; never resend."""


class Refused(KMBoxError):
    """Refused before anything was sent."""


def find_port(setting):
    if setting and setting != "auto":
        return setting
    ports = sorted(glob.glob("/dev/cu.usbserial-*"))
    if not ports:
        raise KMBoxError("no /dev/cu.usbserial-* port: the KMBox's USB(COM) cable is not connected to the Mac "
                         "(or it is in the USB(PC) socket)")
    return ports[0]


class KMBox:
    def __init__(self, cfg, log=None, port_factory=None):
        self.cfg = cfg
        self.log = log or (lambda *a: None)
        self._factory = port_factory
        self.port = None
        self.port_name = None
        self.version = None
        self.scale = None
        self.scale_how = None
        self.origin = (0, 0)
        self._warmed = False
        self._dirty = False
        self.last_used = 0.0
        self.size = tuple(cfg["video_size"])

    # ── session ─────────────────────────────────────────────────────────────────────────────
    def is_open(self):
        return self.port is not None

    def open(self):
        if self.port is not None:
            return
        if self._factory:
            self.port = self._factory()
            self.port_name = "fake"
        else:
            import serial
            self.port_name = find_port(self.cfg.get("serial_port", "auto"))
            try:
                self.port = serial.Serial(self.port_name, self.cfg.get("serial_baud", 115200),
                                          timeout=0.5, write_timeout=2)
            except Exception as e:
                self.port = None
                raise KMBoxError("cannot open %s: %s" % (self.port_name, e))
        self._warmed = False
        self._dirty = False
        try:
            time.sleep(0.2)
            self.port.write(b"\x03")
            time.sleep(0.3)
            self.port.reset_input_buffer()
            self.send("import km", timeout=3)
            self.send("km.Screen(%d,%d)" % self.size)
            self.send_optional("km.trace_enable(0)")
            self.send_optional("km.set_mode(0)")          # 10.3.0 curve setting; absent on 15.x
            mode = self.cfg.get("kmbox_set_mode", 1)
            if mode is not None:
                self.send_optional("km.mode(%d)" % int(mode))
            self.version = self.send_optional("km.version()")
        except Exception:
            self.close()
            raise
        self.last_used = time.time()
        self.log("kmbox: opened %s (%s)" % (self.port_name, self.version))

    def close(self):
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None

    def _drain(self):
        """After a command that never answered, wait for its prompt so replies line up again."""
        self.port.timeout = 5
        self.port.read_until(PROMPT)
        self.port.reset_input_buffer()
        self._dirty = False

    def send(self, command, settle=0.0, timeout=2.0):
        if self.port is None:
            self.open()
        if self._dirty:
            self._drain()
        self.port.write(command.encode() + b"\r\n")
        if settle:
            time.sleep(settle)
        self.port.timeout = timeout
        raw = self.port.read_until(PROMPT)
        self.last_used = time.time()
        reply = raw.decode("utf-8", "replace")
        if not raw.endswith(PROMPT):
            self._dirty = True
            raise MaybeExecuted("KMBox did not answer %r within %.1fs; it may have executed" % (command[:60], timeout))
        reply = reply.replace(command, "", 1).replace(">>>", "").strip()
        if "Traceback" in reply or "Error" in reply:
            raise Rejected("KMBox rejected %r: %s" % (command[:60], reply[:200]))
        return reply

    def send_optional(self, command, settle=0.05):
        try:
            return self.send(command, settle)
        except Rejected:
            return None

    # ── pointer ─────────────────────────────────────────────────────────────────────────────
    def park(self):
        steps = int(self.cfg.get("park_steps", 14))
        delta = -400 if steps >= 14 else -(max(self.size) // max(1, steps - 1) + 200)
        for _ in range(steps):
            self.send("km.move(%d,%d)" % (delta, delta), 0.06)
        time.sleep(0.35 if steps >= 14 else 0.25)

    def load_scale(self):
        """The cached scale, shared with kvm_native.py (same file, same format), if fresh and sane."""
        if os.environ.get("KVM_SCALE"):
            self.scale, self.scale_how = float(os.environ["KVM_SCALE"]), "env override"
            return self.scale
        f = self.cfg["scale_file"]
        lo, hi = self.cfg["scale_range"]
        try:
            st = json.loads(open(f).read())
            age = time.time() - time.mktime(time.strptime(st["measured_at"], "%Y-%m-%dT%H:%M"))
            if lo <= float(st["scale"]) <= hi and 0 <= age < self.cfg["scale_max_age"]:
                self.scale, self.scale_how = float(st["scale"]), "cached %dm old" % (age // 60)
                return self.scale
        except Exception:
            pass
        return None

    def last_scale(self):
        try:
            s = float(json.loads(open(self.cfg["scale_file"]).read())["scale"])
            lo, hi = self.cfg["scale_range"]
            return s if lo <= s <= hi else None
        except Exception:
            return None

    def save_scale(self, scale):
        f = self.cfg["scale_file"]
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with open(f, "w") as fh:
            fh.write(json.dumps({"scale": round(scale, 4), "measured_at": time.strftime("%Y-%m-%dT%H:%M"),
                                 "by": "routec"}))

    def calibrate(self, get_frame, delta=300, attempts=3):
        """Measure frame pixels per KMBox unit by finding the cursor after a known move.

        Repeated up to `attempts` times; two measurements that agree within 3% win. A hardware mouse
        jiggler or a hover highlight makes a blob that is not cursor-sized and is rejected. Values
        outside scale_range are rejected too (29 Oct: a 'measured' 4.607)."""
        from PIL import ImageChops
        lo, hi = self.cfg["scale_range"]
        ox, oy = self.origin
        W, H = self.size
        found = []
        for _ in range(attempts):
            self.park()
            ref = get_frame(time.time() + 0.15)
            self.send("km.move(%d,%d)" % (delta, delta), 0.5)
            cur = get_frame(time.time() + 0.2)
            region = (ox, oy, min(W, ox + delta * 7), min(H, oy + delta * 7))
            a = ref.image().crop(region).convert("L")
            b = cur.image().crop(region).convert("L")
            d = ImageChops.difference(a, b).point(lambda v: 255 if v > 60 else 0)
            d.paste(0, (0, 0, 60, 60))              # the parked cursor's old position
            bbox = d.getbbox()
            if bbox and (bbox[2] - bbox[0]) <= 90 and (bbox[3] - bbox[1]) <= 100:
                s = (bbox[0] / float(delta) + bbox[1] / float(delta)) / 2
                if lo <= s <= hi:
                    found.append(s)
                    if len(found) >= 2 and abs(found[-1] - found[-2]) / found[-2] < 0.03:
                        break
        if len(found) >= 2 and abs(found[-1] - found[-2]) / found[-2] < 0.03:
            self.scale, self.scale_how = (found[-1] + found[-2]) / 2, "measured (2 agreeing)"
            self.save_scale(self.scale)
        elif len(found) == 1:
            self.scale, self.scale_how = found[0], "measured (single reading)"
            self.save_scale(self.scale)
        else:
            prev = self.last_scale()
            if found:
                self.scale, self.scale_how = sum(found) / len(found), "measured (readings disagree: %s)" % (
                    [round(v, 3) for v in found])
            else:
                self.scale = prev or 1.5
                self.scale_how = "FALLBACK to %s: cursor not found (jiggler or hover?). Confirm clicks by " \
                                 "reading the screen." % ("last measured %.3f" % prev if prev else "default 1.5")
        return self.scale, self.scale_how

    def ensure_scale(self, get_frame):
        if self.scale is None and self.load_scale() is None:
            self.calibrate(get_frame)
        return self.scale, self.scale_how

    def moveto(self, x, y):
        if self.scale is None:
            raise KMBoxError("no pointer scale: calibrate first")
        ox, oy = self.origin
        self.park()
        self.send("km.move(%d,%d)" % (round((x - ox) / self.scale), round((y - oy) / self.scale)), 0.45)
        time.sleep(0.15)

    def nudge(self):
        """Tiny move out and back: wakes a sleeping display without clicking anything."""
        self.send("km.move(6,0)", 0.1)
        self.send("km.move(-6,0)", 0.1)

    def click(self, x, y, button="left", mods=(), count=1):
        self.moveto(x, y)
        try:
            for m in mods:
                self.send("km.down(%r)" % m, 0.04)
            if count and count > 1:
                self.send("km.click(%d,%d)" % (BUTTON_NUM[button], count), 0.12)
            else:
                self.send("km.%s(1)" % button, 0.06)
                self.send("km.%s(0)" % button, 0.15)
        finally:
            for m in reversed(list(mods)):
                try:
                    self.send("km.up(%r)" % m)
                except KMBoxError:
                    pass

    def wheel(self, n):
        self.send("km.wheel(%d)" % int(n), 0.4)

    # ── keyboard ────────────────────────────────────────────────────────────────────────────
    def _warmup(self):
        if not self._warmed:
            self.send("km.press('shift-l')", 0.08)
            time.sleep(0.35)
            self._warmed = True

    def _emit(self, *names):
        self._warmup()
        if len(names) == 1:
            self.send("km.press(%r)" % names[0], 0.09)
        else:
            self.send("km.multipress(%s)" % ",".join(repr(n) for n in names), 0.12)

    def key(self, *names, allow_bare=False):
        names = [str(n).lower() for n in names]
        bad = [n for n in names if n not in VALID_KEYS]
        if bad:
            raise Refused("unknown key name(s) %s (backspace is 'back', control is 'ctr-l'). Nothing was sent."
                          % bad)
        if len(names) == 1 and names[0] in FORBIDDEN_BARE and not allow_bare:
            raise Refused("refusing bare %r: in Laurel that is Release/Delete outside a text field. Nothing "
                          "was sent." % names[0])
        if len(names) == 1 and names[0] in ("gui-l", "gui-r"):
            self.tap(names[0])
            return
        self._emit(*names)

    def tap(self, name, hold=0.12):
        self._warmup()
        self.send("km.down(%r)" % name, 0.02)
        time.sleep(hold)
        self.send("km.up(%r)" % name, 0.05)

    @staticmethod
    def untypable(text):
        return [c for c in text if not (c in STRINGABLE or c in NAMED or c in PLAIN or c in SHIFTED
                                        or (c.isalpha() and c.lower() in PLAIN))]

    def type(self, text, progress=None):
        """Type ASCII text. Refuses up front (nothing sent) if any character cannot be produced.
        progress(n) is called with the number of characters sent so far."""
        bad = self.untypable(text)
        if bad:
            raise Refused("cannot type %r on this key table (ASCII only). Nothing was sent." % "".join(sorted(set(bad))))
        self._warmup()
        run = []
        done = [0]

        def flush():
            if not run:
                return
            s = "".join(run)
            self.send("km.string(%r)" % s, timeout=3.0 + 0.08 * len(s))
            time.sleep(0.06)
            done[0] += len(s)
            run.clear()
            if progress:
                progress(done[0])

        for ch in text:
            if ch in STRINGABLE:
                run.append(ch)
                if len(run) >= 40:          # bounded runs: progress stays accurate if the link dies
                    flush()
                continue
            flush()
            if ch in NAMED:
                self._emit(NAMED[ch])
            elif ch == "`" or ch == "~":
                self.send("km.string(%r)" % ch)   # press()/multipress() cannot emit backtick
                time.sleep(0.06)
            elif ch in PLAIN:
                self._emit(ch)
            elif ch.isalpha():
                self._emit("shift-l", ch.lower())
            else:
                self._emit("shift-l", SHIFTED[ch])
            done[0] += 1
            if progress:
                progress(done[0])
        flush()
        return done[0]
