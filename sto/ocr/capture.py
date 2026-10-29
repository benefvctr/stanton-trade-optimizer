"""Screen capture of the calibrated region, plus image prep for OCR."""
from __future__ import annotations

import ctypes

from PIL import Image, ImageOps

_dpi_set = False


def _mss():
    import mss

    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


def make_dpi_aware() -> None:
    """Use physical pixels so calibrated coordinates match mss captures on
    scaled displays."""
    global _dpi_set
    if _dpi_set:
        return
    _dpi_set = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def monitors() -> list[dict]:
    """mss monitor list; index 0 is the whole virtual screen, 1.. are displays."""
    make_dpi_aware()
    with _mss() as sct:
        return [dict(m) for m in sct.monitors]


def grab(monitor: int, region: tuple[int, int, int, int] | None = None) -> Image.Image:
    """Capture a region (left, top, width, height relative to the monitor), or
    the whole monitor when region is None. Read-only screen capture."""
    make_dpi_aware()
    with _mss() as sct:
        mons = sct.monitors
        if monitor < 1 or monitor >= len(mons):
            raise ValueError(f"monitor {monitor} not found (have 1..{len(mons) - 1})")
        mon = mons[monitor]
        if region is None:
            box = dict(mon)
        else:
            x, y, w, h = region
            box = {"left": mon["left"] + x, "top": mon["top"] + y, "width": w, "height": h}
        shot = sct.grab(box)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


TARGET_TEXT_PX = (30, 45, 20, 60, 15, 25, 38, 52)
REGION_TO_TEXT = 3.4   # a calibrated region is about 3.4 text-heights tall
MAX_OCR_SIDE = 10000   # Windows.Media.Ocr's limit


def grab_rect(left: int, top: int, width: int, height: int) -> Image.Image:
    """Capture a rectangle in virtual-screen coordinates (e.g. a window)."""
    make_dpi_aware()
    with _mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def focused_window(title_prefix: str) -> tuple[int, int, int, int] | None:
    """(left, top, width, height) of the foreground window when its title
    starts with title_prefix, else None. Reads the window title and position
    only; never opens the game's process."""
    from ctypes import wintypes

    make_dpi_aware()
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    if not buf.value.startswith(title_prefix):
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    return (rect.left, rect.top, width, height) if width > 200 and height > 200 else None


def ocr_variants(image: Image.Image):
    """Windows OCR reads the small italic balance at some text sizes and misses
    it at others, so yield the region resized to several text heights
    (high-contrast grayscale, plus colour at the first). Sizes follow the
    region's own height, so any monitor resolution gets the same treatment.
    Callers stop at the first parse."""
    base = image.convert("RGB")
    gray = ImageOps.autocontrast(ImageOps.grayscale(base)).convert("RGB")
    text_px = max(base.height / REGION_TO_TEXT, 1)
    for i, target in enumerate(TARGET_TEXT_PX):
        s = target / text_px
        size = (max(1, round(base.width * s)), max(1, round(base.height * s)))
        if max(size) > MAX_OCR_SIDE or min(size) < 8:
            continue
        yield gray.resize(size, Image.LANCZOS)
        if i == 0:
            yield base.resize(size, Image.LANCZOS)
