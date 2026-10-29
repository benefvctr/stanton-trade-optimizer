"""F1 -> wait for the mobiGlas -> capture the calibrated region -> OCR -> parse
-> sanity rules -> callback. Every read is logged to logs/ocr.log.

Game interaction is limited to a global key listener that never suppresses
the key, and a screen capture of one small region. No memory reads, no input
injection, no hooks into the game process.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image

from ..config import LOG_DIR
from . import capture
from .engine import OcrUnavailable, make_engine
from .locate import find_balance
from .parse import parse_balance
from .sanity import WalletSanity


def _monitor_size(monitor: int) -> tuple[int, int]:
    m = capture.monitors()[monitor]
    return m["width"], m["height"]


@dataclass(frozen=True)
class Read:
    at: float
    text: str
    value: int | None
    accepted: bool
    reason: str
    engine: str


def ocr_logger(path: Path = LOG_DIR / "ocr.log") -> logging.Logger:
    log = logging.getLogger("sto.ocr")
    if not log.handlers:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


class WalletSync:
    def __init__(self, *, region: Callable[[], tuple[int, tuple[int, int, int, int]] | None],
                 sanity: WalletSanity, on_read: Callable[[Read], None], hotkey: str = "f1",
                 delay_s: float = 1.2, engine_kind: str = "auto", tesseract_cmd: str = "",
                 grab: Callable[..., Image.Image] = capture.grab, engine=None,
                 log: logging.Logger | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 on_region_moved: Callable[[int, tuple[int, int, int, int]], None] | None = None,
                 monitor_size: Callable[[int], tuple[int, int]] | None = None, locate=None):
        self.region = region
        self.on_region_moved = on_region_moved
        self.monitor_size = monitor_size or _monitor_size
        self.locate = locate or find_balance
        self.sanity = sanity
        self.on_read = on_read
        self.hotkey = hotkey
        self.delay_s = delay_s
        self.engine_kind = engine_kind
        self.tesseract_cmd = tesseract_cmd
        self.grab = grab
        self._engine = engine
        self.log = log or ocr_logger()
        self.sleep = sleep
        self.clock = clock
        self._busy = threading.Lock()
        self._hook = None

    # hotkey

    def start(self) -> None:
        import keyboard

        self._hook = keyboard.on_press_key(self.hotkey, self._pressed, suppress=False)
        self.log.info("listening for %s (delay %.1fs)", self.hotkey.upper(), self.delay_s)

    def stop(self) -> None:
        if self._hook is not None:
            import keyboard

            keyboard.unhook(self._hook)
            self._hook = None

    def _pressed(self, _event=None) -> None:
        # Holding the key repeats it; one read at a time.
        if self._busy.acquire(blocking=False):
            threading.Thread(target=self._delayed_read, daemon=True).start()

    def _delayed_read(self) -> None:
        try:
            self.sleep(self.delay_s)
            self.read_screen()
        finally:
            self._busy.release()

    # reading

    def engine(self):
        if self._engine is None:
            self._engine = make_engine(self.engine_kind, self.tesseract_cmd)
        return self._engine

    def read_screen(self) -> Read:
        where = self.region()
        if where is None:
            return self._finish("", None, False, "not calibrated (press c)", "-")
        monitor, box = where
        try:
            image = self.grab(monitor, box)
            engine = self.engine()
        except OcrUnavailable as e:
            return self._finish("", None, False, f"OCR unavailable: {e}", "-")
        except Exception as e:  # noqa: BLE001 - a failed capture is just a rejected read
            return self._finish("", None, False, f"capture failed: {e}", "-")
        value, raw = self._ocr(image, engine)
        if value is None and self.on_region_moved is not None:
            moved = self._relocate(monitor, box, engine)
            if moved:
                new_box, value, text = moved
                raw += f" | moved to {new_box}: {text}"
                self.on_region_moved(monitor, new_box)
        accepted, reason = self.sanity.offer(value)
        return self._finish(raw, value, accepted, reason, engine.name)

    def read_image(self, image: Image.Image, apply: bool = True) -> Read:
        """OCR an image through the same path as a live read. With apply=False
        (--test-ocr) the sanity rules and callback are skipped."""
        try:
            engine = self.engine()
        except OcrUnavailable as e:
            return self._finish("", None, False, f"OCR unavailable: {e}", "-", apply)
        value, raw = self._ocr(image, engine)
        if not apply:
            return self._finish(raw, value, value is not None, "test", engine.name, apply)
        accepted, reason = self.sanity.offer(value)
        return self._finish(raw, value, accepted, reason, engine.name, apply)

    def _ocr(self, image: Image.Image, engine) -> tuple[int | None, str]:
        texts = []
        for variant in capture.ocr_variants(image):
            text = engine.read(variant)
            texts.append(text)
            value = parse_balance(text)
            if value is not None:
                return value, " | ".join(t.replace("\n", " / ") for t in texts)
        return None, " | ".join(t.replace("\n", " / ") for t in texts)

    def _relocate(self, monitor: int, box: tuple[int, int, int, int], engine):
        """The hologram can sit a little differently between openings. Search
        the area around the old box; returns (new box, value, text) or None.
        A closed mobiGlas finds nothing here, so closing presses stay rejected."""
        x, y, w, h = box
        try:
            mw, mh = self.monitor_size(monitor)
            bx, by = max(0, x - w), max(0, y - 2 * h)
            band = (bx, by, min(mw, x + 2 * w) - bx, min(mh, y + 3 * h) - by)
            found = self.locate(self.grab(monitor, band), engine, near=(x - bx, y - by, w, h))
        except Exception:  # noqa: BLE001
            return None
        if found is None:
            return None
        fx, fy, fw, fh = found.region
        return (bx + fx, by + fy, fw, fh), found.value, found.text

    def _finish(self, text: str, value: int | None, accepted: bool, reason: str, engine: str,
                apply: bool = True) -> Read:
        read = Read(self.clock(), text, value, accepted, reason, engine)
        self.log.info("%s value=%s reason=%s engine=%s raw=%r",
                      "ACCEPT" if accepted else "REJECT", value, reason, engine, text)
        if apply:
            self.on_read(read)
        return read
