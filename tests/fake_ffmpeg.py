"""Stands in for ffmpeg in tests: writes raw RGB frames to stdout.

argv: CONTROL_FILE W H FPS. Each frame it reads CONTROL_FILE for the scene:
  live | live2 (live + a big block) | cursor:X,Y (live + a small square) | black | white
  stall (stop writing, stay alive) | exit (print a lock error, exit 1) | silent (never write)
"""
import sys
import time

from PIL import Image, ImageDraw

ctl, W, H, fps = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])


def scene(name):
    if name == "black":
        return Image.new("RGB", (W, H), (0, 0, 0))
    if name == "white":
        return Image.new("RGB", (W, H), (250, 250, 250))
    img = Image.new("RGB", (W, H), (40, 44, 52))
    d = ImageDraw.Draw(img)
    for i in range(0, W, 40):
        d.rectangle([i, 10, i + 25, 30], fill=(200, 200, 200))
    d.rectangle([20, H // 2, W // 2, H // 2 + 20], fill=(90, 160, 220))
    if name == "live2":
        d.rectangle([W // 4, H // 4, W * 3 // 4, H * 3 // 4], fill=(230, 120, 30))
    if name.startswith("cursor:"):
        x, y = (int(v) for v in name.split(":")[1].split(","))
        d.rectangle([x, y, x + 4, y + 6], fill=(255, 255, 255))
    return img


first = True
while True:
    try:
        name = open(ctl).read().strip() or "live"
    except OSError:
        name = "live"
    if name == "exit":
        sys.stderr.write("[avfoundation] Could not lock device for configuration\n")
        sys.stderr.flush()
        sys.exit(1)
    if name in ("stall", "silent"):
        time.sleep(0.2)
        continue
    sys.stdout.buffer.write(scene(name).tobytes())
    sys.stdout.buffer.flush()
    time.sleep(1.0 / fps)
