"""Configuration: defaults that match Eugene's Mac, overridable by a JSON file.

Override file: $ROUTEC_CONFIG, else ~/.config/routec/config.json. Only the keys you set change.
Paths starting with ~ are expanded.
"""
import json
import os
from pathlib import Path

PROXY = "~/Claude/Projects/Proxy"

DEFAULTS = {
    # HTTP API. Loopback only; every API call also needs the token in token_file.
    "host": "127.0.0.1",
    "port": 8790,
    "token_file": "~/Library/Application Support/RouteC/token",

    # Capture: the Elgato 4K X as an AVFoundation (UVC) device, read by one long-lived ffmpeg.
    "ffmpeg": "/opt/homebrew/bin/ffmpeg",
    "device": "Elgato 4K X",
    "video_size": [3840, 2160],      # pinned: a negotiated format drifts to 1080p (17 Sep 2026)
    "pixel_format": "nv12",
    "framerate": 30,                 # what the device is opened at
    "stream_fps": 2,                 # what we keep (ffmpeg's fps filter drops the rest)
    "warmup_seconds": 1.5,           # frames this soon after (re)open can be black; never served
    "stall_seconds": 8,              # no frame for this long -> kill and reopen the stream
    "idle_stop_seconds": 0,          # 0 = stream always on; N = stop after N s with no client
    "capture_cmd": None,             # tests: replace the ffmpeg command line entirely
    "obs_fallback": True,            # if the device is locked by OBS, read frames from OBS instead

    # KMBox serial REPL.
    "serial_port": "auto",           # auto = first /dev/cu.usbserial-*
    "serial_baud": 115200,
    "serial_idle_close": 20,         # close the port after this many idle seconds (lets kvm_native.py in)
    "kmbox_set_mode": 1,             # km.mode(1) = USB mode, mouse + keyboard composite (21 Sep 2026)

    # Eugene's work folder; obs_capture.py is borrowed from here for the OBS fallback.
    "proxy_dir": PROXY,

    # Shared with kvm_native.py so the two never fight: same lease file, same scale cache.
    "lease_file": PROXY + "/.kvm-test/route-c.lease",
    "scale_file": PROXY + "/.kvm-test/kvm_state.json",
    "scale_max_age": 1200,
    "scale_range": [0.8, 3.0],       # a "measured" 4.607 on 29 Oct sent clicks to half-coordinates

    # Hard limits.
    "unattended_cutoff": "07:00",    # unattended input leases end here on weekdays
    "presence_window": 15,           # seconds of no human-looking change before input is allowed
    "own_input_grace": 4,            # screen changes this soon after our own input are ours

    # Where things are written.
    "state_dir": "~/Library/Application Support/RouteC",
    "log_dir": "~/Library/Logs/RouteC",
    # Snapshots go inside the Proxy folder so device_stage_files / Desktop Commander read_file reach them.
    "snapshot_dir": PROXY + "/.kvm-test/routec",
    "snapshot_keep": 60,             # keep only the newest N snapshots (they contain client material)

    # OCR.
    "ocr_engine": "vision",          # vision | tesseract. Tesseract reads are labelled DEGRADED.
    "tesseract": "/opt/homebrew/bin/tesseract",
}

_PATH_KEYS = {"token_file", "proxy_dir", "lease_file", "scale_file", "state_dir", "log_dir", "snapshot_dir",
              "ffmpeg", "tesseract"}


class Config(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def path(self, key):
        return Path(os.path.expanduser(self[key]))


def config_file():
    env = os.environ.get("ROUTEC_CONFIG")
    return Path(os.path.expanduser(env)) if env else Path.home() / ".config" / "routec" / "config.json"


def load(overrides=None):
    cfg = Config(DEFAULTS)
    f = config_file()
    if f.exists():
        cfg.update(json.loads(f.read_text()))
    if overrides:
        cfg.update(overrides)
    for k in _PATH_KEYS:
        if isinstance(cfg.get(k), str):
            cfg[k] = os.path.expanduser(cfg[k])
    cfg["video_size"] = list(cfg["video_size"])
    return cfg
