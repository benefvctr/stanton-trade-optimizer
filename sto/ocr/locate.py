"""Find the balance on a mobiGlas frame by OCR, so nobody has to drag a box
over the game (Star Citizen keeps the mouse while it has focus).

The app bar along the bottom of the mobiGlas has fixed tab labels (HOME,
HEALTH, COMMS ... VEHICLES). The balance sits on that bar, left of the tabs.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from PIL import Image

from .capture import MAX_OCR_SIDE, ocr_variants
from .parse import parse_balance

TABS = {"HOME", "HEALTH", "COMMS", "CONTRACTS", "MAPS", "JOURNAL", "ASSETS", "REP", "WALLET", "LANDING",
        "VEHICLES"}
MIN_DIGITS = 3
# Zoom passes. Words from every pass are pooled: on one frame the tab labels
# may only be read at one zoom and the number at another.
FRAME_SCALES = (1, 0.5, 2, 0.75, 1.5)
BAND_SCALES = (2, 1, 3, 1.5, 0.75)


@dataclass(frozen=True)
class Word:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def digits(self) -> int:
        return sum(ch.isdigit() for ch in self.text)


@dataclass(frozen=True)
class Found:
    region: tuple[int, int, int, int]   # x, y, width, height in the searched image
    value: int
    text: str


def _words(image: Image.Image, engine, scale: float, offset=(0, 0)) -> list[Word]:
    size = (round(image.width * scale), round(image.height * scale))
    if max(size) > MAX_OCR_SIDE:
        return []
    big = image if scale == 1 else image.resize(size, Image.LANCZOS)
    ox, oy = offset
    return [Word(t, x / scale + ox, y / scale + oy, w / scale, h / scale) for t, x, y, w, h in engine.words(big)]


def _pad(word: Word, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """A box around the number with OCR-friendly margins and room to grow."""
    h = word.h
    x0, x1 = max(0.0, word.x - 1.2 * h), min(size[0], word.right + 3 * h)
    y0, y1 = max(0.0, word.y - 1.2 * h), min(size[1], word.y + 2.2 * h)
    return round(x0), round(y0), round(x1 - x0), round(y1 - y0)


def read_region(image: Image.Image, region: tuple[int, int, int, int], engine) -> tuple[int | None, str]:
    """Parse the balance in one region through the same variants a live read uses."""
    x, y, w, h = region
    crop = image.crop((x, y, x + w, y + h))
    texts = []
    for variant in ocr_variants(crop):
        text = engine.read(variant)
        texts.append(text)
        value = parse_balance(text)
        if value is not None:
            return value, text
    return None, " | ".join(texts)


def _frame_candidates(words: list[Word], size: tuple[int, int]) -> list[Word] | None:
    """Number-like words on the app bar left of its tabs; None when fewer than
    two tab labels are visible (the mobiGlas isn't open)."""
    tabs = [w for w in words if w.text.strip(".,:;'").upper() in TABS and w.cy > size[1] * 0.5]
    if len({t.text.strip(".,:;'").upper() for t in tabs}) < 2:
        return None
    bar_y = statistics.median(t.cy for t in tabs)
    tab_h = statistics.median(t.h for t in tabs)
    first_tab = min(t.x for t in tabs)
    return sorted((w for w in words if w.digits >= MIN_DIGITS and abs(w.cy - bar_y) <= 4 * tab_h
                   and w.right <= first_tab + tab_h),
                  key=lambda w: (-w.digits, abs(w.cy - bar_y)))


def find_balance(image: Image.Image, engine, near: tuple[int, int, int, int] | None = None) -> Found | None:
    """Locate the balance and return a region around it.

    Without `near`, the whole frame is searched and the mobiGlas must be
    recognisable by at least two app-bar tab labels; None means it isn't open.
    With `near` (a previous region), only the area around it is searched, to
    follow small shifts of the hologram between reads.
    """
    image = image.convert("RGB")
    size = image.size
    tried: set[tuple[int, int]] = set()
    words: list[Word] = []
    if near is not None:
        nx, ny, nw, nh = near
        bx0, by0 = max(0, nx - nw), max(0, ny - 2 * nh)
        bx1, by1 = min(size[0], nx + 2 * nw), min(size[1], ny + 3 * nh)
        if bx1 <= bx0 or by1 <= by0:
            return None
        band = image.crop((bx0, by0, bx1, by1))
        ncx, ncy = nx + nw / 2, ny + nh / 2
    for scale in (BAND_SCALES if near is not None else FRAME_SCALES):
        if near is not None:
            words += _words(band, engine, scale, offset=(bx0, by0))
            candidates = sorted((w for w in words if w.digits >= MIN_DIGITS),
                                key=lambda w: (w.cx - ncx) ** 2 + (w.cy - ncy) ** 2)
        else:
            words += _words(image, engine, scale)
            candidates = _frame_candidates(words, size)
            if candidates is None:
                continue
        for word in candidates:
            key = (round(word.cx / max(word.h, 1)), round(word.cy / max(word.h, 1)))
            if key in tried:          # the same word seen at another zoom
                continue
            tried.add(key)
            region = _pad(word, size)
            value, text = read_region(image, region, engine)
            if value is not None:
                return Found(region, value, text)
    return None


def scale_region(region: tuple[int, int, int, int], from_size: tuple[int, int],
                 to_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Map a region found on a resized screenshot onto the real monitor."""
    sx, sy = to_size[0] / from_size[0], to_size[1] / from_size[1]
    x, y, w, h = region
    return round(x * sx), round(y * sy), round(w * sx), round(h * sy)
