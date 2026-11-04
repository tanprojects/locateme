"""Frames and frame health.

Every frame the daemon serves carries a health verdict, so "the screen is blank" is never a guess:
the verdict says whether the *laptop* is sending a black/uniform picture (asleep, locked, no
signal) or whether the picture is live, and the capture layer separately says whether frames are
flowing at all. Classification runs on a 240x135 thumbnail and costs a few milliseconds.
"""
import time

from PIL import Image, ImageChops, ImageStat

THUMB_FACTOR = 16          # 3840x2160 -> 240x135
PRESENCE_SIZE = (960, 540)


class Frame:
    __slots__ = ("seq", "ts", "size", "data", "source", "_img", "_thumb", "_gray", "health")

    def __init__(self, seq, ts, size, data=None, source="ffmpeg", image=None):
        self.seq = seq
        self.ts = ts
        self.size = tuple(size)
        self.data = data
        self.source = source
        self._img = image
        self._thumb = None
        self._gray = None
        self.health = None

    def image(self):
        if self._img is None:
            self._img = Image.frombuffer("RGB", self.size, self.data, "raw", "RGB", 0, 1)
        return self._img

    def thumb(self):
        if self._thumb is None:
            self._thumb = self.image().reduce(THUMB_FACTOR).convert("L")
        return self._thumb

    def small(self):
        """RGB at 960x540, for change detection (same scale the old presence() used)."""
        if self._gray is None:
            img = self.image()
            factor = max(1, img.width // PRESENCE_SIZE[0])
            g = img.reduce(factor) if factor > 1 else img.copy()
            if g.size != PRESENCE_SIZE:
                g = g.resize(PRESENCE_SIZE)
            self._gray = g
        return self._gray

    def age(self, now=None):
        return (now or time.time()) - self.ts

    def meta(self):
        return {"seq": self.seq, "ts": round(self.ts, 3), "age": round(self.age(), 2),
                "size": list(self.size), "source": self.source, "health": self.health}


def classify(frame):
    """Return the frame's health: {"state", "mean", "stddev", "content_box"?, "detail"}.

    state:
      live         normal picture
      black        uniformly black: the laptop is sending black (display asleep / off / screen
                   saver) or the HDMI signal is gone. Frames ARE arriving: not a capture fault.
      uniform      one flat colour that is not black (a capture card slate, a blank white screen)
      letterboxed  content inside black bars: the laptop's output mode no longer fills the frame
    """
    t = frame.thumb()
    st = ImageStat.Stat(t)
    mean, std = st.mean[0], st.stddev[0]
    lo, hi = t.getextrema()
    out = {"mean": round(mean, 1), "stddev": round(std, 1)}
    if hi < 16 and std < 3:
        out.update(state="black", detail="laptop picture is uniformly black (display asleep/off, "
                                         "screen saver, or HDMI signal lost); frames are arriving")
        return out
    if std < 4:
        out.update(state="uniform", detail="laptop picture is one flat colour (mean %.0f): blank "
                                           "screen or capture-card slate" % mean)
        return out
    box = _content_box(t)
    if box:
        f = THUMB_FACTOR
        x0, y0, x1, y1 = box
        cb = [x0 * f, y0 * f, x1 * f, y1 * f]
        w, h = frame.size
        if (cb[2] - cb[0]) < w * 0.97 or (cb[3] - cb[1]) < h * 0.97:
            out.update(state="letterboxed", content_box=cb,
                       detail="picture fills only %dx%d of the %dx%d frame (black bars): the laptop's "
                              "display mode changed" % (cb[2] - cb[0], cb[3] - cb[1], w, h))
            return out
    out.update(state="live", detail="live picture")
    return out


def _content_box(thumb, dark=14):
    """Bounding box of everything brighter than `dark`, only if the margins around it are truly
    black (a letterbox), else None."""
    mask = thumb.point(lambda v: 255 if v > dark else 0)
    bbox = mask.getbbox()
    if not bbox:
        return None
    w, h = thumb.size
    x0, y0, x1, y1 = bbox
    margin = max(x0, w - x1, y0, h - y1)
    if margin < 3:            # content reaches (almost) every edge: not letterboxed
        return None
    return bbox


def diff_summary(a, b, threshold=40):
    """Compare two frames at 960x540. Returns {"changed": fraction, "bbox": frame coords or None}.
    Uses the largest per-channel difference, so a colour change of similar brightness still counts."""
    ga, gb = a.small(), b.small()
    r, g, b_ = ImageChops.difference(ga, gb).split()
    d = ImageChops.lighter(ImageChops.lighter(r, g), b_).point(lambda v: 255 if v > threshold else 0)
    bbox = d.getbbox()
    if not bbox:
        return {"changed": 0.0, "bbox": None}
    n = d.histogram()[255]
    sx = a.size[0] / ga.size[0]
    sy = a.size[1] / ga.size[1]
    fb = [int(bbox[0] * sx), int(bbox[1] * sy), int(bbox[2] * sx), int(bbox[3] * sy)]
    return {"changed": round(n / float(ga.size[0] * ga.size[1]), 5), "bbox": fb,
            "small_bbox": list(bbox)}
