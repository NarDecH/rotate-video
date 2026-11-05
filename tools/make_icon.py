"""Generate assets/app.ico used for the Windows executable."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
BG = (37, 99, 235)
FG = (255, 255, 255)


def rounded_rect(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = int(size * 0.22)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=BG)
    return img


def draw_arrow(img: Image.Image) -> None:
    draw = ImageDraw.Draw(img)
    size = img.size[0]
    cx = cy = size / 2
    outer = size * 0.31
    inner = size * 0.20
    width = max(2, int(size * 0.065))

    # circular arc (from 300 deg to 200 deg, clockwise gap on the right)
    bbox = (cx - outer, cy - outer, cx + outer, cy + outer)
    draw.arc(bbox, start=-60, end=250, fill=FG, width=width)

    # arrow head at the end of the arc (250 degrees)
    angle = math.radians(250)
    ax = cx + outer * math.cos(angle)
    ay = cy + outer * math.sin(angle)
    head = size * 0.11
    # tangent direction (clockwise along the arc)
    tx, ty = -math.sin(angle), math.cos(angle)
    nx, ny = math.cos(angle), math.sin(angle)
    p1 = (ax + tx * head * 0.9, ay + ty * head * 0.9)
    p2 = (ax - tx * head * 0.35 + nx * head * 0.8,
          ay - ty * head * 0.35 + ny * head * 0.8)
    p3 = (ax - tx * head * 0.35 - nx * head * 0.8,
          ay - ty * head * 0.35 - ny * head * 0.8)
    draw.polygon([p1, p2, p3], fill=FG)

    # small centre square = "screen"
    box = size * 0.085
    draw.rounded_rectangle((cx - box / 2, cy - box / 2, cx + box / 2, cy + box / 2),
                           radius=int(size * 0.02), fill=FG)


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    large = rounded_rect(SIZE)
    draw_arrow(large)
    large.save(out_dir / "app.ico",
               sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    large.save(out_dir / "app.png")
    print("wrote", out_dir / "app.ico")


if __name__ == "__main__":
    main()
