"""Daemon + CLI end to end: fake ffmpeg, fake KMBox, fake OCR, real HTTP, real job queue."""
import json
import os
import threading
import time

import pytest

from routec import cli
from routec.engine import Engine
from routec.server import App, LocalHTTP, make_handler
from conftest import FakePort, set_scene, wait_until


class FakeOCR:
    def __init__(self):
        self.words = [("Inbox", 40, 60), ("Save", 200, 120), ("draft", 240, 120)]

    def lines(self, pil, fast=False):
        f = pil.width / 320.0
        out = []
        for text, x, y in self.words:
            w = {"text": text, "x": int(x * f), "y": int(y * f), "w": int(30 * f), "h": int(10 * f)}
            out.append({"text": text, "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"], "conf": 1.0, "words": [w]})
        return out


@pytest.fixture
def daemon(cfg, tmp_path, monkeypatch):
    port = FakePort()
    ocr = FakeOCR()
    httpd = LocalHTTP(("127.0.0.1", 0), None)
    cfg["port"] = httpd.server_address[1]
    open(cfg["scale_file"], "w").write(json.dumps({"scale": 1.5, "measured_at": time.strftime("%Y-%m-%dT%H:%M")}))
    eng = Engine(cfg, km_factory=lambda: port, ocr_engine=ocr)
    app = App(cfg, eng)
    httpd.RequestHandlerClass = make_handler(app)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps({k: v for k, v in cfg.items()}))
    monkeypatch.setenv("ROUTEC_CONFIG", str(conf))
    monkeypatch.setenv("ROUTEC_OWNER", "tester")
    eng.capture.get(timeout=10)
    yield {"cfg": cfg, "port": port, "ocr": ocr, "eng": eng, "app": app}
    httpd.shutdown()
    eng.capture.stop()


def run(capsys, *argv):
    code = cli.main(list(argv))
    o = capsys.readouterr()
    return code, o.out, o.err


def test_status_and_reads(daemon, capsys):
    code, out, err = run(capsys, "status")
    assert code == 0 and out.startswith("OK: live 320x180")
    code, out, err = run(capsys, "screen")
    assert "Inbox" in out and code == 0
    code, out, _ = run(capsys, "find", "Save draft")
    hits = json.loads(out)
    assert len(hits) == 1 and hits[0]["text"] == "Save draft"
    code, out, _ = run(capsys, "look")
    assert os.path.exists(out.strip())


def test_black_screen_is_explained_not_blank(daemon, capsys):
    set_scene(daemon["cfg"], "black")
    wait_until(lambda: daemon["eng"].capture.latest().health["state"] == "black")
    code, out, err = run(capsys, "status")
    assert code == 5 and "LAPTOP PICTURE BLACK" in out
    code, out, err = run(capsys, "screen")
    assert "WARNING: laptop picture is black" in err


def test_input_requires_lease(daemon, capsys):
    code, out, err = run(capsys, "click", "100", "100")
    assert code == 1
    r = json.loads(out)
    assert r["status"] == "refused" and r["code"] == "lease" and r["may_have_executed"] is False
    assert not daemon["port"].commands("km.left")


def test_click_type_and_idempotent_retry(daemon, capsys):
    assert run(capsys, "lease", "tester", "5")[0] == 0
    code, out, err = run(capsys, "click", "300", "150", "--id", "c1")
    r = json.loads(out)
    assert code == 0 and r["ok"] is True, out + err
    assert "km.move(200,100)" in daemon["port"].sent
    n_clicks = len(daemon["port"].commands("km.left(1)"))
    # the transport "timed out", Claude asks again with the same id: nothing is re-sent
    code, out, err = run(capsys, "click", "300", "150", "--id", "c1")
    assert "NOT run again" in err and len(daemon["port"].commands("km.left(1)")) == n_clicks
    code, out, err = run(capsys, "type", "Hello World")
    assert code == 0, out + err
    assert "km.string(' ')" in daemon["port"].sent or "km.string('ello ')" in daemon["port"].sent
    code, out, err = run(capsys, "release", "tester")
    assert code == 0


def test_password_prompt_blocks_typing(daemon, capsys):
    run(capsys, "lease", "tester", "5")
    daemon["ocr"].words = [("Password", 100, 80)]
    before = len(daemon["port"].sent)
    code, out, err = run(capsys, "type", "hunter2")
    r = json.loads(out)
    assert r["code"] == "password_guard" and r["may_have_executed"] is False
    assert not any(c.startswith("km.string") for c in daemon["port"].sent[before:])


def test_presence_blocks_input(daemon, capsys):
    run(capsys, "lease", "tester", "5")
    daemon["cfg"]["presence_window"] = 3
    set_scene(daemon["cfg"], "live2")
    time.sleep(0.4)
    set_scene(daemon["cfg"], "live")
    time.sleep(0.4)
    code, out, err = run(capsys, "click", "10", "10")
    r = json.loads(out)
    assert r["code"] == "human_active", out


def test_batch_with_wait_and_assert(daemon, capsys, tmp_path):
    run(capsys, "lease", "tester", "5")
    steps = [{"do": "assert", "text": "Inbox"}, {"do": "click_text", "text": "Save draft"},
             {"do": "wait", "text": "Inbox", "timeout": 3}, {"do": "refute", "text": "Inbox"},
             {"do": "type", "text": "never typed"}]
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    code, out, err = run(capsys, "run", str(f))
    r = json.loads(out)
    assert code == 1 and r["status"] == "failed"
    assert r["partial"]["stopped_at"] == 3
    assert r["may_have_executed"] is False
    assert not any("never" in c for c in daemon["port"].sent)


def test_bad_batch_sends_nothing(daemon, capsys, tmp_path):
    run(capsys, "lease", "tester", "5")
    f = tmp_path / "steps.json"
    f.write_text(json.dumps([{"do": "click", "x": 1, "y": 1}, {"do": "key", "keys": ["r"]}]))
    before = len(daemon["port"].sent)
    code, out, err = run(capsys, "run", str(f))
    assert code == 1 and "Laurel" in err
    assert len(daemon["port"].sent) == before


def test_token_and_host_checks(daemon):
    import urllib.request, urllib.error
    base = "http://127.0.0.1:%d" % daemon["cfg"]["port"]
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/status")
    assert e.value.code == 401
    req = urllib.request.Request(base + "/ping", headers={"Host": "evil.example:80"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
