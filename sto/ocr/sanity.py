"""Accept or reject OCR balance reads."""
from __future__ import annotations


class WalletSanity:
    """A read within big_change_pct of the last accepted value is accepted at
    once. A bigger jump needs two matching reads in a row. Failed reads in
    between don't reset that: pressing F1 again to close the mobiGlas always
    produces one."""

    def __init__(self, big_change_pct: float = 60, last: int | None = None):
        self.big_change_pct = big_change_pct
        self.last = last
        self.pending: int | None = None

    def reset(self, value: int | None) -> None:
        """A manual entry or other trusted value becomes the new baseline."""
        self.last = value
        self.pending = None

    def offer(self, value: int | None) -> tuple[bool, str]:
        if value is None:
            return False, "not a number"
        if self.last is None:
            self.reset(value)
            return True, "accepted"
        change = abs(value - self.last) / max(self.last, 1) * 100
        if change <= self.big_change_pct:
            self.reset(value)
            return True, "accepted"
        if self.pending == value:
            self.reset(value)
            return True, "accepted (confirmed)"
        self.pending = value
        return False, f"{change:.0f}% change, waiting for a second matching read"
