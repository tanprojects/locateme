"""OCR: Apple Vision, in-process, on frames already in memory.

The tiling and band-merge are the proven ones from Proxy/vision_ocr.py and kvm_native.lines()
(13 Oct 2026): Vision silently downsamples big images, so a 4K frame is read as overlapping
1280x720 tiles, thin strips are upscaled, tile duplicates are dropped, and observations sharing a
horizontal band are merged into one line so a list row reads as one line.

Keeping Vision loaded in the daemon saves the ~1s PyObjC import every CLI call used to pay.
If Vision fails, tesseract is used and every result says engine "tesseract (DEGRADED)".
"""
import io
import re
import subprocess
import tempfile
import threading

from PIL import Image

VISION = "vision"
DEGRADED = "tesseract (DEGRADED)"


def _iou(a, b):
    ax0, ay0, ax1, ay1 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
    bx0, by0, bx1, by1 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    iw = max(0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    if not inter:
        return 0.0
    return inter / float(a["w"] * a["h"] + b["w"] * b["h"] - inter)


def _overlap_frac(a, b):
    ax0, ay0, ax1, ay1 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
    bx0, by0, bx1, by1 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    iw = max(0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0, min(ay1, by1) - max(ay0, by0))
    return (iw * ih) / float(max(1, a["w"] * a["h"]))


class VisionEngine:
    def __init__(self):
        import Quartz  # noqa: F401  (pyobjc; present in the 3.13 framework python on the Mac)
        import Vision
        from Foundation import NSData
        self.Quartz = Quartz
        self.Vision = Vision
        self.NSData = NSData

    def _cgimage(self, pil):
        buf = io.BytesIO()
        pil.save(buf, "PNG", compress_level=1)
        data = self.NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))
        src = self.Quartz.CGImageSourceCreateWithData(data, None)
        return self.Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)

    def lines(self, pil, fast=False, langs=("en-AU", "en-US")):
        img = self._cgimage(pil)
        W, H = pil.size
        V = self.Vision
        req = V.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1 if fast else 0)
        req.setUsesLanguageCorrection_(False)
        req.setRecognitionLanguages_(list(langs))
        handler = V.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError("Vision failed: %s" % err)

        def px(bb):
            x = bb.origin.x * W
            y = (1.0 - bb.origin.y - bb.size.height) * H
            return int(round(x)), int(round(y)), int(round(bb.size.width * W)), int(round(bb.size.height * H))

        out = []
        for obs in req.results() or []:
            cand = obs.topCandidates_(1)[0]
            text = cand.string()
            x, y, w, h = px(obs.boundingBox())
            words, start = [], 0
            for tok in text.split(" "):
                if not tok:
                    start += 1
                    continue
                bb = cand.boundingBoxForRange_error_(V.NSMakeRange(start, len(tok)), None)
                if isinstance(bb, tuple):
                    bb = bb[0]
                if bb is not None:
                    wx, wy, ww, wh = px(bb.boundingBox())
                    words.append({"text": tok, "x": wx, "y": wy, "w": ww, "h": wh})
                start += len(tok) + 1
            out.append({"text": text, "x": x, "y": y, "w": w, "h": h,
                        "conf": float(cand.confidence()), "words": words})
        return out


class TesseractEngine:
    def __init__(self, path):
        self.path = path

    def lines(self, pil, fast=False):
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            pil.save(f.name)
            tsv = subprocess.run([self.path, f.name, "-", "--psm", "6", "tsv"], capture_output=True,
                                 text=True, errors="replace", timeout=60).stdout
        groups = {}
        for row in tsv.splitlines()[1:]:
            c = row.split("\t")
            if len(c) < 12 or c[0] != "5" or not c[11].strip():
                continue
            w = {"text": c[11], "x": int(c[6]), "y": int(c[7]), "w": int(c[8]), "h": int(c[9])}
            groups.setdefault((c[1], c[2], c[3], c[4]), []).append(w)
        out = []
        for words in groups.values():
            x = min(w["x"] for w in words); y = min(w["y"] for w in words)
            x1 = max(w["x"] + w["w"] for w in words); y1 = max(w["y"] + w["h"] for w in words)
            out.append({"text": " ".join(w["text"] for w in words), "x": x, "y": y, "w": x1 - x,
                        "h": y1 - y, "conf": 0.5, "words": words})
        return out


class OCR:
    def __init__(self, cfg, log=None, engine=None):
        self.cfg = cfg
        self.log = log or (lambda *a: None)
        self._lock = threading.Lock()
        self._vision = None
        self._vision_error = None
        self._engine_override = engine        # tests inject a fake engine
        self._tess = TesseractEngine(cfg.get("tesseract", "tesseract"))

    def engine(self):
        """(engine, label). Vision unless it is unavailable or configured off."""
        if self._engine_override is not None:
            return self._engine_override, VISION
        if self.cfg.get("ocr_engine", "vision") == "vision":
            if self._vision is None and self._vision_error is None:
                try:
                    self._vision = VisionEngine()
                except Exception as e:
                    self._vision_error = "%s: %s" % (type(e).__name__, e)
                    self.log("Vision unavailable (%s); OCR will be DEGRADED" % self._vision_error)
            if self._vision is not None:
                return self._vision, VISION
        return self._tess, DEGRADED

    def status(self):
        eng, label = self.engine()
        return {"engine": label, "vision_error": self._vision_error}

    # ── tiling ──────────────────────────────────────────────────────────────────────────────
    def _tiled(self, eng, img, ox, oy, tile=(1280, 720), overlap=(256, 48)):
        W, H = img.size
        up = 3 if H <= 160 else (2 if H <= 400 or W <= 400 else 1)
        tw, th = tile
        sx, sy = max(1, tw - overlap[0]), max(1, th - overlap[1])
        out = []
        y = 0
        while True:
            x = 0
            while True:
                c = img.crop((x, y, min(W, x + tw), min(H, y + th)))
                if up > 1:
                    c = c.resize((c.width * up, c.height * up), Image.LANCZOS)
                for l in eng.lines(c):
                    for k in ("x", "y", "w", "h"):
                        l[k] = int(round(l[k] / up))
                    l["x"] += x + ox
                    l["y"] += y + oy
                    for wd in l["words"]:
                        for k in ("x", "y", "w", "h"):
                            wd[k] = int(round(wd[k] / up))
                        wd["x"] += x + ox
                        wd["y"] += y + oy
                    out.append(l)
                if x + tw >= W:
                    break
                x += sx
            if y + th >= H:
                break
            y += sy
        out.sort(key=lambda l: (-len(l["text"]), -l.get("conf", 0)))
        kept = []
        for l in out:
            if any(_iou(l, k) > 0.5 or (l["w"] * l["h"] and _overlap_frac(l, k) > 0.8) for k in kept):
                continue
            kept.append(l)
        return kept

    @staticmethod
    def _merge_bands(obs):
        obs = sorted(obs, key=lambda l: l["y"] + l["h"] / 2.0)
        bands = []
        for l in obs:
            cy = l["y"] + l["h"] / 2.0
            if bands and abs(cy - bands[-1]["cy"]) <= max(6, 0.45 * max(l["h"], bands[-1]["h"])):
                b = bands[-1]
                b["items"].append(l)
                b["cy"] = (b["cy"] * (len(b["items"]) - 1) + cy) / len(b["items"])
                b["h"] = max(b["h"], l["h"])
            else:
                bands.append({"cy": cy, "h": l["h"], "items": [l]})
        merged = []
        for b in bands:
            words = []
            for l in sorted(b["items"], key=lambda l: l["x"]):
                for w in (l["words"] or [{"text": l["text"], "x": l["x"], "y": l["y"], "w": l["w"], "h": l["h"]}]):
                    if any(_iou(w, k) > 0.4 for k in words):
                        continue
                    words.append(w)
            if not words:
                continue
            words.sort(key=lambda w: w["x"])
            x = min(w["x"] for w in words); y = min(w["y"] for w in words)
            x1 = max(w["x"] + w["w"] for w in words); y1 = max(w["y"] + w["h"] for w in words)
            merged.append({"text": " ".join(w["text"] for w in words), "x": x, "y": y,
                           "w": x1 - x, "h": y1 - y, "words": words})
        return merged

    # ── public ──────────────────────────────────────────────────────────────────────────────
    def lines(self, image, box=None):
        """OCR lines in full-frame coordinates. box = (x0, y0, x1, y1). Returns (lines, engine)."""
        W, H = image.size
        pad = 24          # Vision needs air around a thin strip; padding is filtered back out
        if box:
            ox, oy = max(0, box[0]), max(0, box[1] - pad)
            crop = image.crop((ox, oy, min(W, box[2]), min(H, box[3] + pad)))
        else:
            ox, oy, crop = 0, 0, image
        with self._lock:
            eng, label = self.engine()
            try:
                obs = self._tiled(eng, crop, ox, oy)
            except Exception as e:
                if label != VISION:
                    raise
                self.log("Vision read failed (%s); falling back to tesseract (DEGRADED)" % e)
                eng, label = self._tess, DEGRADED
                obs = self._tiled(eng, crop, ox, oy)
        if box:
            obs = [l for l in obs if box[1] - 6 <= l["y"] + l["h"] // 2 <= box[3] + 6]
        return self._merge_bands(obs), label

    def text(self, image, box=None):
        lines, label = self.lines(image, box)
        return "\n".join(l["text"] for l in lines), label

    def find(self, image, needle, box=None):
        """Locate text; centre of the matched WORDS (not the line). Case/punctuation-insensitive,
        because OCR turns | into [ or l. Returns (hits, engine)."""
        norm = lambda t: re.sub(r"[^a-z0-9@.]+", "", t.lower())
        n = norm(needle)
        if not n:
            return [], self.engine()[1]
        lines, label = self.lines(image, box)
        hits = []
        for r in lines:
            joined, owner = "", []
            for i, w in enumerate(r["words"]):
                t = norm(w["text"])
                joined += t
                owner += [i] * len(t)
            start = joined.find(n)
            while start != -1:
                ws = r["words"][owner[start]:owner[start + len(n) - 1] + 1]
                x = min(w["x"] for w in ws); y = min(w["y"] for w in ws)
                x1 = max(w["x"] + w["w"] for w in ws); y1 = max(w["y"] + w["h"] for w in ws)
                hits.append({"cx": (x + x1) // 2, "cy": (y + y1) // 2, "x": x, "y": y, "w": x1 - x,
                             "h": y1 - y, "text": " ".join(w["text"] for w in ws), "line": r["text"]})
                start = joined.find(n, start + 1)
        return hits, label


LOCK_WORDS = ("sign-in options", "i forgot my password", "ctrl + alt + delete", "ctrl+alt+delete",
              "press ctrl", "this pc is locked", "to unlock", "enter your password", "enter your pin",
              "other user")


def looks_locked(lines):
    """A Windows lock or sign-in screen: few lines, and lock-screen wording."""
    text = " ".join(l["text"].lower() for l in lines)
    hit = [w for w in LOCK_WORDS if w in text]
    return bool(hit) and len(lines) < 30, hit
