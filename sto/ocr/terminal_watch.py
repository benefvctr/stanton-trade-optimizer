"""Watch for a commodity terminal on screen and read it automatically.

Every few seconds, and only while Star Citizen is the focused window: capture
the game window, run a cheap half-resolution check for the terminal's labels,
and when one is up, read the full screen. The same screen isn't reported
twice in a row unless it changed or a while has passed.

Screen capture only, like the F1 balance read: nothing touches the game.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from PIL import Image

from . import capture
from .engine import OcrUnavailable, make_engine
from .sync import ocr_logger
from .terminal import TerminalScreen, detect, read_screen

# () -> (commodity names, typical price per name); empty names = no data yet
Context = Callable[[], tuple[list[str], dict[str, float]]]


class TerminalWatcher:
    def __init__(self, *, context: Context, on_screen: Callable[[TerminalScreen], None],
                 interval_s: float = 3.0, repeat_s: float = 60.0, window_title: str = "Star Citizen",
                 engine_kind: str = "auto", tesseract_cmd: str = "", engine=None,
                 window: Callable[[], tuple[int, int, int, int] | None] | None = None,
                 grab: Callable[[tuple[int, int, int, int]], Image.Image] | None = None,
                 log: logging.Logger | None = None, clock: Callable[[], float] = time.time):
        self.context = context
        self.on_screen = on_screen
        self.interval_s = interval_s
        self.repeat_s = repeat_s
        self.window = window or (lambda: capture.focused_window(window_title))
        self.grab = grab or (lambda rect: capture.grab_rect(*rect))
        self.engine_kind = engine_kind
        self.tesseract_cmd = tesseract_cmd
        self._engine = engine
        self.log = log or ocr_logger()
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sig = None
        self._last_at = 0.0
        self.status = "waiting for a commodity terminal"

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="terminal-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.poll_once()
            except OcrUnavailable as e:
                self.status = f"OCR unavailable: {e}"
                return
            except Exception:  # noqa: BLE001 - one bad frame must not end the watcher
                self.log.exception("terminal watch: poll failed")

    def engine(self):
        if self._engine is None:
            self._engine = make_engine(self.engine_kind, self.tesseract_cmd)
        return self._engine

    def poll_once(self) -> TerminalScreen | None:
        rect = self.window()
        if rect is None:
            return None
        names, references = self.context()
        if not names:
            return None
        image = self.grab(rect)
        engine = self.engine()
        anchors = detect(image, engine)
        if anchors is None:
            self._last_sig = None          # terminal closed: the next opening is new
            return None
        screen = read_screen(image, engine, names, references, anchors=anchors)
        if screen is None or not (screen.buy or screen.sell):
            return None
        sig, now = screen.signature(), self.clock()
        if sig == self._last_sig and now - self._last_at < self.repeat_s:
            return None
        self._last_sig, self._last_at = sig, now
        self.log.info("TERMINAL location=%s buy=%s sell=%s balance=%s", screen.location,
                      [(r.commodity, r.price, r.stock) for r in screen.buy],
                      [(r.commodity, r.price) for r in screen.sell], screen.balance)
        self.on_screen(screen)
        return screen
