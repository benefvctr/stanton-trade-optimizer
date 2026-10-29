"""Number and age formatting for a narrow table."""
from __future__ import annotations

import time

from rich.text import Text

BAR = " ▏▎▍▌▋▊▉█"
GOOD, WARN, BAD = "#7ddc6a", "#ffd23f", "#ff4f3a"


def compact(n: float) -> str:
    """1,234,567 -> 1.23M; 653,989 -> 654k; 9,800 -> 9,800."""
    a = abs(n)
    sign = "-" if n < 0 else ""
    if a >= 10_000_000:
        return f"{sign}{a / 1e6:.1f}M"
    if a >= 1_000_000:
        return f"{sign}{a / 1e6:.2f}M"
    if a >= 10_000:
        return f"{sign}{a / 1e3:.0f}k"
    return f"{n:,.0f}"


def full(n: float | None) -> str:
    return "?" if n is None else f"{n:,.0f}"


def age_text(hours: float) -> str:
    if hours < 1:
        return f"{max(hours * 60, 0):.0f}m"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def age_color(hours: float) -> str:
    return GOOD if hours < 6 else WARN if hours < 48 else BAD


def aged(hours: float) -> Text:
    return Text(age_text(hours), style=age_color(hours))


def conf_bar(c: float, width: int = 4) -> Text:
    """Confidence as a partial-block bar, coloured by level."""
    steps = round(max(0.0, min(1.0, c)) * width * 8)
    full_blocks, rest = divmod(steps, 8)
    bar = "█" * full_blocks + (BAR[rest] if rest and full_blocks < width else "")
    color = GOOD if c >= 0.6 else WARN if c >= 0.25 else BAD
    return Text(bar.ljust(width, "·"), style=color)


def clock(ts: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"


def countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


FLAG_CODES = {"pad ?": "P?", "box ?": "B?", "demand ?": "D?", "dist ~": "~", "stale": "S"}


def flag_text(flags: tuple[str, ...]) -> Text:
    t = Text()
    for f in flags:
        code = FLAG_CODES.get(f, f)
        t.append(code + " ", style=BAD if f == "stale" else WARN if f in ("pad ?", "box ?") else "#8a8a8a")
    return t
