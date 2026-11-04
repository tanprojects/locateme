import pytest

from routec.kmbox import KMBox, MaybeExecuted, Refused, Rejected
from conftest import FakePort


def km_with(cfg, port):
    k = KMBox(cfg, port_factory=lambda: port)
    k.open()
    return k


def test_open_sequence(cfg):
    p = FakePort()
    km_with(cfg, p)
    assert p.sent[:2] == ["import km", "km.Screen(320,180)"]
    assert "km.mode(1)" in p.sent and "km.trace_enable(0)" in p.sent


def test_type_routes_shift_and_runs(cfg):
    p = FakePort()
    k = km_with(cfg, p)
    p.sent.clear()
    k.type("Hi there, $5 ~ok`")
    cmds = p.sent
    assert cmds[0] == "km.press('shift-l')"                       # warm-up
    assert "km.multipress('shift-l','h')" in cmds                 # capital per-char
    assert "km.string('i there, ')" in cmds                       # unshifted run in one call
    assert "km.multipress('shift-l','4')" in cmds                 # $ per-char
    assert "km.string('~')" in cmds and "km.string('`')" in cmds  # backtick route
    assert not any("km.string(" in c and any(ch.isupper() for ch in c[10:]) for c in cmds)


def test_untypable_text_sends_nothing(cfg):
    p = FakePort()
    k = km_with(cfg, p)
    p.sent.clear()
    with pytest.raises(Refused):
        k.type("café")
    assert p.sent == []


def test_bare_release_key_refused(cfg):
    p = FakePort()
    k = km_with(cfg, p)
    p.sent.clear()
    with pytest.raises(Refused):
        k.key("r")
    with pytest.raises(Refused):
        k.key("backspace")
    assert p.sent == []
    k.key("ctr-l", "a")
    assert p.sent[-1] == "km.multipress('ctr-l','a')"


def test_no_prompt_raises_maybe_executed_then_drains(cfg):
    p = FakePort(hang={"km.left(1)"})
    k = km_with(cfg, p)
    with pytest.raises(MaybeExecuted):
        k.send("km.left(1)", timeout=0.1)
    k.send("km.left(0)")             # drains the late prompt first, then lines up again
    assert p.sent[-1] == "km.left(0)"


def test_rejected(cfg):
    p = FakePort(reject={"km.bogus"})
    k = km_with(cfg, p)
    with pytest.raises(Rejected):
        k.send("km.bogus()")


def test_scale_cache_rejects_insane_values(cfg):
    import json, time
    open(cfg["scale_file"], "w").write(json.dumps({"scale": 4.607, "measured_at": time.strftime("%Y-%m-%dT%H:%M")}))
    k = KMBox(cfg, port_factory=FakePort)
    assert k.load_scale() is None
    open(cfg["scale_file"], "w").write(json.dumps({"scale": 1.502, "measured_at": time.strftime("%Y-%m-%dT%H:%M")}))
    assert k.load_scale() == 1.502


def test_moveto_parks_then_moves_by_scale(cfg):
    p = FakePort()
    k = km_with(cfg, p)
    k.scale = 2.0
    p.sent.clear()
    k.moveto(300, 100)
    assert p.sent[-1] == "km.move(150,50)"
    assert all(c.startswith("km.move(-") for c in p.sent[:-1])
