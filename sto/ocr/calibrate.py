"""Set the balance region without touching the game's mouse: find it by OCR on
a live screenshot of each monitor, or on a saved screenshot."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image

from . import capture
from .locate import find_balance, scale_region

SC_SCREENSHOT_DIRS = (
    Path(r"C:\Program Files\Roberts Space Industries\StarCitizen\LIVE\ScreenShots"),
    Path(r"C:\Program Files\Roberts Space Industries\StarCitizen\PTU\ScreenShots"),
)
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


@dataclass(frozen=True)
class Calibration:
    monitor: int
    region: tuple[int, int, int, int]
    value: int
    source: str


def calibrate_screen(engine, preferred: int = 1, monitors: list[dict] | None = None,
                     grab: Callable = capture.grab) -> Calibration | None:
    """Screenshot each monitor (the preferred one first) and look for the open
    mobiGlas. None when no monitor shows it."""
    mons = monitors if monitors is not None else capture.monitors()
    order = [preferred] + [i for i in range(1, len(mons)) if i != preferred]
    for i in order:
        if not 1 <= i < len(mons):
            continue
        found = find_balance(grab(i), engine)
        if found:
            return Calibration(i, found.region, found.value, f"monitor {i}")
    return None


def pick_monitor(size: tuple[int, int], mons: list[dict], preferred: int = 1) -> int:
    """The monitor a screenshot of this size came from: same size first, then
    same shape (for a resized screenshot). Raises ValueError when none fits."""
    displays = list(range(1, len(mons)))
    exact = [i for i in displays if (mons[i]["width"], mons[i]["height"]) == tuple(size)]
    if exact:
        return preferred if preferred in exact else exact[0]
    ratio = size[0] / size[1]
    shaped = [i for i in displays if abs(mons[i]["width"] / mons[i]["height"] - ratio) / ratio < 0.02]
    if shaped:
        return preferred if preferred in shaped else shaped[0]
    raise ValueError(f"a {size[0]}x{size[1]} screenshot doesn't match the shape of any monitor; "
                     "use a full-screen screenshot of the game's monitor")


def calibrate_image(path: Path | str, engine, preferred: int = 1,
                    monitors: list[dict] | None = None) -> Calibration | None:
    """Find the balance on a saved full-screen screenshot and map the region
    onto the monitor it came from (scaling it when the screenshot was resized)."""
    path = Path(path)
    image = Image.open(path).convert("RGB")
    mons = monitors if monitors is not None else capture.monitors()
    monitor = pick_monitor(image.size, mons, preferred)
    found = find_balance(image, engine)
    if not found:
        return None
    target = (mons[monitor]["width"], mons[monitor]["height"])
    region = found.region if image.size == target else scale_region(found.region, image.size, target)
    return Calibration(monitor, region, found.value, path.name)


def newest_screenshot(dirs=SC_SCREENSHOT_DIRS) -> Path | None:
    files = []
    for d in dirs:
        try:
            files += [p for p in Path(d).iterdir() if p.suffix.lower() in IMAGE_TYPES]
        except OSError:
            continue
    return max(files, key=lambda p: p.stat().st_mtime) if files else None
