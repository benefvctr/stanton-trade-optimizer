"""What your trading earned, from the balance readings in order (F1, terminal
screens, manual entries).

A run shows up in the readings as a fall (buying cargo, maybe refuelling) and
then a rise (selling, perhaps in a few batches at one terminal). The run's
earnings are the last balance minus the balance before that fall. Readings can
miss a step: when the rise doesn't follow a fall, it's counted on its own, so
two runs are never added together.
"""
from __future__ import annotations

from dataclasses import dataclass

BATCH_GAP_S = 300   # sells this close together are one sale split into batches


@dataclass(frozen=True)
class Entry:
    at: float
    value: int
    source: str     # "ocr", "terminal" or "manual"


@dataclass(frozen=True)
class Run:
    change: int          # earned (positive) or, while cargo is aboard, spent (negative)
    in_progress: bool    # the last reading went down: cargo bought, not sold yet
    since: float         # time of the balance it's measured from


def last_run(entries: list[Entry]) -> Run | None:
    v = [e.value for e in entries]
    t = [e.at for e in entries]
    i = len(v) - 1
    if i < 1:
        return None
    if v[i] < v[i - 1]:
        k = i
        while k > 0 and v[k - 1] > v[k]:
            k -= 1
        return Run(v[i] - v[k], True, t[k])
    # Back through the rise: its first step can span a whole flight, later
    # steps only count when they're batches of one sale.
    j = i
    while j > 0 and v[j - 1] < v[j]:
        first_step = j - 1 == 0 or v[j - 2] >= v[j - 1]
        if first_step or t[j] - t[j - 1] <= BATCH_GAP_S:
            j -= 1
            if first_step:
                break
        else:
            break
    # Back through the fall before it: buying, and anything paid on the way.
    k = j
    while k > 0 and v[k - 1] > v[k]:
        k -= 1
    if k == i:
        k = i - 1
    return Run(v[i] - v[k], False, t[k])


def session_change(entries: list[Entry]) -> int | None:
    return entries[-1].value - entries[0].value if len(entries) > 1 else None
