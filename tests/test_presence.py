import pytest
from PIL import Image, ImageDraw

from routec.frames import Frame, classify
from routec.presence import HumanActive, Presence


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def mk(ts, extra=None, seq=[0]):
    img = Image.new("RGB", (3840, 2160), (40, 44, 52))
    d = ImageDraw.Draw(img)
    for i in range(0, 3840, 200):
        d.rectangle([i, 100, i + 120, 300], fill=(220, 220, 220))
    if extra:
        d.rectangle(extra, fill=(255, 0, 0))
    seq[0] += 1
    f = Frame(seq[0], ts, img.size, img.tobytes())
    f.health = classify(f)
    return f


def test_quiet_then_active_then_own_input(cfg):
    clk = Clock()
    p = Presence(cfg, clock=clk)
    cfg["presence_window"] = 5
    p.feed(mk(clk.t))
    clk.t += 6
    p.feed(mk(clk.t))
    assert p.report()["active"] is False and "quiet" in p.report()["verdict"]
    p.require_quiet()
    # cursor-sized change: ignored
    clk.t += 1
    p.feed(mk(clk.t, [1000, 1000, 1012, 1018]))
    assert p.report()["active"] is False
    # big change: someone is working
    clk.t += 1
    p.feed(mk(clk.t, [500, 500, 1500, 900]))
    assert p.report()["active"] is True
    with pytest.raises(HumanActive):
        p.require_quiet()
    # our own input: not counted
    clk.t += 10
    p.own_input()
    p.feed(mk(clk.t, [200, 1200, 1800, 1600]))
    assert p.report()["active"] is False


def test_taskbar_clock_ignored(cfg):
    clk = Clock()
    p = Presence(cfg, clock=clk)
    p.feed(mk(clk.t))
    clk.t += 1
    p.feed(mk(clk.t, [3500, 2050, 3700, 2140]))
    assert p.report()["last_activity"] is None


def test_unknown_history_refuses(cfg):
    clk = Clock()
    p = Presence(cfg, clock=clk)
    cfg["presence_window"] = 30
    p.feed(mk(clk.t))
    with pytest.raises(HumanActive):
        p.require_quiet(wait_history=False)
