import time

import pytest

from routec.capture import CaptureError, FrameStream, classify_stderr, verdict
from conftest import set_scene, wait_until


@pytest.fixture
def stream(cfg):
    s = FrameStream(cfg)
    s.start()
    yield s
    s.stop()


def test_frames_flow_and_are_live(stream):
    f = stream.get(timeout=8)
    assert f.size == (320, 180)
    assert f.health["state"] == "live"
    st = stream.status()
    assert st["state"] == "streaming"
    assert verdict(st).startswith("OK")


def test_fresh_read_waits_for_a_newer_frame(stream):
    f1 = stream.get(timeout=8)
    t = time.time()
    f2 = stream.get(fresh_after=t, timeout=3)
    assert f2.ts > t and f2.seq > f1.seq


def test_black_laptop_is_reported_as_laptop_not_capture(cfg, stream):
    stream.get(timeout=8)
    set_scene(cfg, "black")
    f = wait_until(lambda: (lambda x: x if x.health["state"] == "black" else None)(stream.get(fresh_after=time.time())))
    assert f.health["state"] == "black"
    v = verdict(stream.status())
    assert "CAPTURE OK" in v and "BLACK" in v


def test_stall_is_detected_restarted_and_never_served_stale(cfg, stream):
    stream.get(timeout=8)
    set_scene(cfg, "stall")
    time.sleep(stream.max_age() + 0.3)
    with pytest.raises(CaptureError):
        stream.get(timeout=0.5)
    assert wait_until(lambda: stream.status()["error"] and stream.status()["error"]["code"] == "stalled", 6)
    restarts = stream.status()["restarts"]
    set_scene(cfg, "live")
    f = stream.get(timeout=10)
    assert f.health["state"] == "live"
    assert stream.status()["restarts"] > restarts or stream.status()["error"] is None


def test_no_frames_at_all_names_the_cause(cfg):
    set_scene(cfg, "silent")
    s = FrameStream(cfg)
    s.start()
    try:
        with pytest.raises(CaptureError):
            s.get(timeout=1)
        st = wait_until(lambda: s.status()["error"], 8)
        assert st["code"] == "no_frames"
        assert "Camera permission" in st["hint"]
        assert verdict(s.status()).startswith("NO PICTURE")
    finally:
        s.stop()


def test_device_busy_from_stderr(cfg):
    set_scene(cfg, "exit")
    s = FrameStream(cfg)
    s.start()
    try:
        err = wait_until(lambda: s.status()["error"], 6)
        assert err["code"] == "device_busy"
    finally:
        s.stop()


def test_warmup_frames_never_served(cfg):
    cfg["warmup_seconds"] = 0.8
    set_scene(cfg, "black")
    s = FrameStream(cfg)
    s.start()
    try:
        time.sleep(0.4)
        set_scene(cfg, "live")
        f = s.get(timeout=8)
        assert f.ts - s._proc_started >= 0.8
    finally:
        s.stop()


def test_classify_stderr():
    assert classify_stderr(["Could not lock device for configuration"]) == "device_busy"
    assert classify_stderr(["Selected video size (3840x2160) is not supported by the device"]) == "format_unsupported"
    assert classify_stderr(["Video device not found"]) == "device_missing"
    assert classify_stderr(["harmless"]) is None
