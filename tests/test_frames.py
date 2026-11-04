from PIL import Image, ImageDraw

from routec.frames import Frame, classify, diff_summary


def frame_of(img, seq=1):
    return Frame(seq, 0.0, img.size, img.tobytes())


def busy(size=(3840, 2160)):
    img = Image.new("RGB", size, (40, 44, 52))
    d = ImageDraw.Draw(img)
    for i in range(0, size[0], 200):
        d.rectangle([i, 100, i + 120, 300], fill=(220, 220, 220))
    return img


def test_black_uniform_live():
    assert classify(frame_of(Image.new("RGB", (3840, 2160))))["state"] == "black"
    assert classify(frame_of(Image.new("RGB", (3840, 2160), (0, 0, 200))))["state"] == "uniform"
    assert classify(frame_of(busy()))["state"] == "live"


def test_letterbox_detected_with_content_box():
    img = Image.new("RGB", (3840, 2160))
    img.paste(busy((3840, 1620)), (0, 270))
    h = classify(frame_of(img))
    assert h["state"] == "letterboxed"
    x0, y0, x1, y1 = h["content_box"]
    assert abs(y0 - 270) <= 16 and abs(y1 - 1890) <= 16 and x0 == 0


def test_dark_ui_is_not_mistaken_for_black():
    img = Image.new("RGB", (3840, 2160), (31, 31, 31))
    ImageDraw.Draw(img).text((100, 100), "Inbox", fill=(230, 230, 230))
    ImageDraw.Draw(img).rectangle([300, 300, 900, 360], fill=(200, 200, 200))
    assert classify(frame_of(img))["state"] == "live"


def test_diff_summary():
    a = busy()
    b = a.copy()
    ImageDraw.Draw(b).rectangle([1000, 1000, 2000, 1500], fill=(255, 0, 0))
    d = diff_summary(frame_of(a), frame_of(b))
    assert d["changed"] > 0.01
    x0, y0, x1, y1 = d["bbox"]
    assert 990 <= x0 <= 1010 and 1490 <= y1 <= 1510
    assert diff_summary(frame_of(a), frame_of(a.copy()))["bbox"] is None
