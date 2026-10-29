"""Turn a read terminal screen into stored price reports for a UEX terminal.

The terminal never names itself. The left panel shows the location when one
of your inventories is selected ("BAIJINI POINT"); otherwise the set of
commodities on sale and their prices single out the terminal: at Port Tressler
7 captured buy rows matched exactly one UEX terminal.
"""
from __future__ import annotations

import difflib
import statistics
from dataclasses import dataclass

from .models import Dataset
from .ocr.terminal import TerminalScreen


@dataclass(frozen=True)
class Identified:
    terminal_id: int | None
    candidates: tuple[str, ...]      # labels of the best few, for messages
    reason: str


def commodity_references(ds: Dataset) -> dict[str, float]:
    """Typical UEX price per commodity name, the anchor OCR readings are checked against."""
    values: dict[int, list[float]] = {}
    for p in ds.prices:
        for v in (p.price_buy, p.price_sell):
            if v > 0:
                values.setdefault(p.commodity_id, []).append(v)
    return {ds.commodities[c].name: statistics.median(v) for c, v in values.items() if c in ds.commodities}


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.upper(), b.upper()).ratio()


def identify_terminal(ds: Dataset, screen: TerminalScreen) -> Identified:
    names = {c.name.upper(): c.id for c in ds.commodities.values()}
    buys = {names[r.commodity.upper()]: r.price for r in screen.buy if r.price and r.commodity.upper() in names}
    sells = {names[r.commodity.upper()]: r.price for r in screen.sell if r.price and r.commodity.upper() in names}
    if not buys and not sells:
        return Identified(None, (), "no prices read")

    candidates = [t for t in ds.terminals.values() if t.is_live]
    narrowed = False
    if screen.location:
        here = [t for t in candidates
                if max(_similar(screen.location, t.place), _similar(screen.location, t.label),
                       _similar(screen.location, t.name)) >= 0.8]
        if here:
            candidates, narrowed = here, True

    index = {(p.terminal_id, p.commodity_id): p for p in ds.prices}
    scored = []
    for t in candidates:
        score = 0.0
        for side, rows in (("buy", buys), ("sell", sells)):
            for cid, price in rows.items():
                p = index.get((t.id, cid))
                known = p.price_buy if (p and side == "buy") else (p.price_sell if p else 0)
                if known > 0:
                    score += 1 + max(0.0, 1 - abs(known - price) / known)   # up to 2 per row
                else:
                    score -= 1
        scored.append((score, t))
    scored.sort(key=lambda st: st[0], reverse=True)
    if not scored:
        return Identified(None, (), "no candidate terminals")
    best_score, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else float("-inf")
    rows = len(buys) + len(sells)
    labels = tuple(t.label for _, t in scored[:3])
    if narrowed and len(scored) == 1 and best_score >= 0:
        return Identified(best.id, labels, "location")
    if best_score >= max(2.0, rows) and best_score - second >= 1.0:
        return Identified(best.id, labels, "location and prices" if narrowed else "prices")
    return Identified(None, labels, "ambiguous")


def capture_rows(ds: Dataset, screen: TerminalScreen, terminal_id: int, at: float) -> list[dict]:
    """Rows for Store.save_captures. Only rows with a price are kept: a stock
    figure next to a stale UEX price would look fresher than it is."""
    names = {c.name.upper(): c.id for c in ds.commodities.values()}
    rows = []
    for r in screen.buy:
        cid = names.get(r.commodity.upper())
        if cid and r.price:
            rows.append({"terminal_id": terminal_id, "commodity_id": cid, "side": "buy", "price": r.price,
                         "scu": r.stock, "status": r.status,
                         "boxes": ",".join(map(str, r.boxes)) if r.boxes else None, "captured_at": at})
    for r in screen.sell:
        cid = names.get(r.commodity.upper())
        if cid and r.price:
            rows.append({"terminal_id": terminal_id, "commodity_id": cid, "side": "sell", "price": r.price,
                         "scu": None, "status": None, "boxes": None, "captured_at": at})
    return rows
