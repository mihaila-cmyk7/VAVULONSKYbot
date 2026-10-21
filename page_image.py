"""Рисует «скриншот» страницы библиотеки: 40 строк по 80 символов, найденный текст подсвечен."""
from __future__ import annotations

import io
import logging
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

COLS = 80
FONT_SIZE = 20
MARGIN = 28
HEADER_H = 64
BG = (250, 246, 236)
INK = (35, 30, 25)
HILITE = (255, 214, 64)
MUTED = (120, 110, 95)

_FONT_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DejaVuSansMono.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "DejaVuSansMono.ttf",
)


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    log.warning("Моноширинный шрифт не найден, использую встроенный")
    try:
        return ImageFont.load_default(size)
    except TypeError:  # старая Pillow
        return ImageFont.load_default()


def render_page(
    page_text: str,
    match_start: Optional[int],
    match_len: int,
    title: str,
    subtitle: str = "",
) -> io.BytesIO:
    font = _load_font(FONT_SIZE)
    small = _load_font(16)
    cw = font.getlength("M")
    ascent, descent = font.getmetrics()
    lh = ascent + descent + 4

    lines = [page_text[i:i + COLS] for i in range(0, len(page_text), COLS)] or [""]
    w = int(cw * COLS + MARGIN * 2)
    h = int(HEADER_H + lh * len(lines) + MARGIN)
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)

    d.text((MARGIN, 14), title, font=font, fill=INK)
    if subtitle:
        d.text((MARGIN, 40), subtitle, font=small, fill=MUTED)
    d.line((MARGIN, HEADER_H - 6, w - MARGIN, HEADER_H - 6), fill=MUTED, width=1)

    # подсветка найденного фрагмента (может занимать несколько строк)
    if match_start is not None and match_len > 0:
        end = match_start + match_len
        for row in range(len(lines)):
            a, b = row * COLS, row * COLS + len(lines[row])
            lo, hi = max(a, match_start), min(b, end)
            if lo >= hi:
                continue
            x0 = MARGIN + (lo - a) * cw
            x1 = MARGIN + (hi - a) * cw
            y0 = HEADER_H + row * lh
            d.rectangle((x0, y0, x1, y0 + lh - 2), fill=HILITE)

    for row, line in enumerate(lines):
        y = HEADER_H + row * lh
        for col, ch in enumerate(line):
            if ch != " ":
                d.text((MARGIN + col * cw, y), ch, font=font, fill=INK)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = "babel_page.png"
    return buf
