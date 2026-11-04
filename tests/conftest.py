import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from routec import config as config_mod  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    ctl = tmp_path / "scene"
    ctl.write_text("live")
    c = config_mod.load({
        "video_size": [320, 180],
        "stream_fps": 10,
        "warmup_seconds": 0.3,
        "stall_seconds": 1.5,
        "capture_cmd": [sys.executable, os.path.join(ROOT, "tests", "fake_ffmpeg.py"), str(ctl), "{w}", "{h}", "{fps}"],
        "obs_fallback": False,
        "token_file": str(tmp_path / "token"),
        "lease_file": str(tmp_path / "route-c.lease"),
        "scale_file": str(tmp_path / "kvm_state.json"),
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "logs"),
        "snapshot_dir": str(tmp_path / "snaps"),
        "presence_window": 1.0,
        "own_input_grace": 0.5,
        "park_steps": 2,
        "port": 0,
    })
    c["_scene"] = str(ctl)
    return c


def set_scene(cfg, name):
    with open(cfg["_scene"], "w") as f:
        f.write(name)


class FakePort:
    """A KMBox MicroPython REPL: echoes each command and answers with the prompt.

    hang: set of command prefixes that never answer; reject: prefixes that raise a Traceback."""

    def __init__(self, hang=(), reject=()):
        self.sent = []
        self.hang = set(hang)
        self.reject = set(reject)
        self.timeout = 0.5
        self._pending = b""
        self.closed = False

    def write(self, data):
        cmd = data.decode().strip()
        if cmd == "\x03" or data == b"\x03":
            return len(data)
        self.sent.append(cmd)
        if any(cmd.startswith(p) for p in self.hang):
            self._pending = b""
            self._late = (cmd + "\r\n>>> ").encode()
            return len(data)
        if any(cmd.startswith(p) for p in self.reject):
            self._pending = (cmd + "\r\nTraceback (most recent call last):\r\nNameError: x\r\n>>> ").encode()
        else:
            reply = "kmbox: 15.0.0 fake" if cmd == "km.version()" else ""
            self._pending = (cmd + "\r\n" + reply + ("\r\n" if reply else "") + ">>> ").encode()
        return len(data)

    def read_until(self, expected=b">>> "):
        if self._pending:
            out, self._pending = self._pending, b""
            return out
        late = getattr(self, "_late", None)
        if late and self.timeout >= 5:      # a drain waits long enough for the late prompt
            self._late = None
            return late
        time.sleep(min(self.timeout, 0.05))
        return b""

    def reset_input_buffer(self):
        self._pending = b""

    def close(self):
        self.closed = True

    def commands(self, prefix=""):
        return [c for c in self.sent if c.startswith(prefix)]


def wait_until(fn, timeout=8.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()
