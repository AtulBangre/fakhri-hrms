#!/usr/bin/env python3
"""Generate the PWA icons in the Fakhri brand maroon.

    ./.venv/bin/python3 scripts/make_icons.py

Produces ``app/static/img/icon-192.png``, ``icon-512.png`` and
``maskable-512.png``. The mark is a monogram "F" — deliberately **not** a copy of
the company logo, because the real logo file belongs to Fakhri IT Services.

To use the real logo instead, drop your file in as
``app/static/img/logo.png`` and edit ``templates/base.html`` /
``templates/login.html`` to point the ``brand-mark`` at it, or just overwrite
``icon-512.png`` with a square version of your logo.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "app" / "static" / "img"

BRAND = (0x87, 0x08, 0x08)       # primary
BRAND_DARK = (0x6F, 0x06, 0x06)  # gradient end
WHITE = (255, 255, 255)

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/SFNS.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


def _load_font(size: int) -> ImageFont.ImageFont | None:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return None


def _gradient(size: int) -> Image.Image:
    """Diagonal maroon gradient, drawn row by row (cheap and smooth enough)."""
    image = Image.new("RGB", (size, size), BRAND)
    draw = ImageDraw.Draw(image)
    for y in range(size):
        for_ratio = y / max(1, size - 1)
        colour = tuple(
            int(BRAND[i] + (BRAND_DARK[i] - BRAND[i]) * for_ratio) for i in range(3)
        )
        draw.line([(0, y), (size, y)], fill=colour)
    # A subtle diagonal sheen so the icon does not look flat.
    sheen = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    sheen_draw = ImageDraw.Draw(sheen)
    sheen_draw.polygon(
        [(0, size), (size * 0.62, 0), (size, 0), (0, size * 0.72)],
        fill=(255, 255, 255, 16),
    )
    return Image.alpha_composite(image.convert("RGBA"), sheen).convert("RGB")


def _draw_monogram(image: Image.Image, *, font_ratio: float = 0.62) -> Image.Image:
    size = image.width
    draw = ImageDraw.Draw(image)
    font = _load_font(int(size * font_ratio))

    if font is not None:
        text = "F"
        box = draw.textbbox((0, 0), text, font=font)
        width, height = box[2] - box[0], box[3] - box[1]
        draw.text(
            ((size - width) / 2 - box[0], (size - height) / 2 - box[1]),
            text,
            font=font,
            fill=WHITE,
        )
        return image

    # No usable font (common in slim Linux containers): draw a geometric "F".
    stroke = max(4, int(size * 0.085))
    left = int(size * 0.34)
    top = int(size * 0.27)
    height = int(size * 0.46)
    bar_width = int(size * 0.30)
    arm_width = int(size * 0.22)
    draw.rectangle([left, top, left + stroke, top + height], fill=WHITE)
    draw.rectangle([left, top, left + bar_width, top + stroke], fill=WHITE)
    draw.rectangle([left, top + int(height * 0.44), left + arm_width, top + int(height * 0.44) + stroke], fill=WHITE)
    return image


def make_icon(size: int, *, maskable: bool = False) -> Image.Image:
    image = _gradient(size)
    if maskable:
        # Maskable icons must survive a circular crop: keep the mark inside a
        # 60% safe area and let the background bleed to the edges.
        inner = int(size * 0.62)
        canvas = _gradient(size)
        mark = _draw_monogram(_gradient(inner))
        canvas.paste(mark, ((size - inner) // 2, (size - inner) // 2))
        return canvas
    return _draw_monogram(image)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = [
        ("icon-192.png", make_icon(192)),
        ("icon-512.png", make_icon(512)),
        ("maskable-512.png", make_icon(512, maskable=True)),
        ("apple-touch-icon.png", make_icon(180)),
    ]
    for name, image in targets:
        path = OUT_DIR / name
        image.save(path, "PNG", optimize=True)
        print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
