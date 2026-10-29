"""SC Trade Tools community price reports: the free, documented feed.

GET https://sc-trade.tools/api/crowdsource/commodity-listings?page=N lists every
price report players submit, newest first, 100 per page, no token needed. The
reports are raw: the site's own docs call them "unfiltered and may contain
outliers", and many carry the currency sign misread as a leading digit (copper
54,400 next to the real 3,700). So a report is used only when it lands near the
UEX price for that commodity, and only when it is newer than what UEX has (the
overlay in models.apply_captures decides that).

Gateway stations come without their system ("nyx gateway" exists in Stanton and
in Pyro), so a place that matches several terminals is settled by price.
"""
from __future__ import annotations

import http.client
import json
import math
import re
import statistics
import time
from collections import Counter
from datetime import datetime
from typing import Callable

from .models import Commodity, Price, Terminal
from .uex import Transport, urllib_transport

URL = "https://sc-trade.tools/api/crowdsource/commodity-listings?page={page}"
USER_AGENT = "stanton-trade-optimizer (+https://github.com/benefvctr/stanton-trade-optimizer)"
MIN_RATIO, MAX_RATIO = 0.5, 2.0       # a report this far off the UEX price is a misread
SIDES = {"SELLS": "buy", "BUYS": "sell"}   # the shop sells = you buy there


class SctradeError(Exception):
    pass


def _time(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_listing(x: dict) -> dict | None:
    """One report as a Store row, or None when it is malformed or has no price."""
    try:
        side = SIDES.get(x["transaction"])
        price = float(x["price"] or 0)
        at = _time(x["timestamp"])
        location, commodity = str(x["location"]).strip().lower(), str(x["commodity"]).strip().lower()
    except (KeyError, TypeError, ValueError):
        return None
    if not side or price <= 0 or at is None or not location or not commodity:
        return None
    qty, sat, boxes = x.get("quantity"), x.get("saturation"), x.get("boxSizesInScu")
    return {"location": location, "commodity": commodity, "side": side, "price": price,
            "scu": int(qty) if isinstance(qty, (int, float)) else None,
            "saturation": float(sat) if isinstance(sat, (int, float)) else None,
            "boxes": ",".join(str(int(b)) for b in boxes) if boxes else None, "observed_at": at}


def fetch_listings(since: float, oldest: float, max_pages: int = 10, transport: Transport = urllib_transport,
                   sleep: Callable[[float], None] = time.sleep, pace_s: float = 2.0,
                   timeout: float = 30.0) -> list[dict]:
    """Reports newer than `since` (the newest one already stored) and not older
    than `oldest`. Pages are newest first, so this usually costs one request."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    out: list[dict] = []
    for page in range(max_pages):
        if page:
            sleep(pace_s)
        try:
            code, body = transport(URL.format(page=page), headers, timeout)
        except (OSError, http.client.HTTPException) as e:
            raise SctradeError(f"SC Trade Tools: network error: {e!r}"[:300]) from e
        if code != 200:
            raise SctradeError(f"SC Trade Tools: HTTP {code}")
        try:
            content = json.loads(body)["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise SctradeError("SC Trade Tools: the response was not the expected JSON") from e
        rows = [r for r in map(parse_listing, content) if r]
        out += [r for r in rows if r["observed_at"] > since and r["observed_at"] >= oldest]
        if not content or not rows or min(r["observed_at"] for r in rows) <= max(since, oldest):
            break
    return out


# --- matching reports to UEX terminals ---------------------------------------------------

def _plain(name: str, system: str) -> str:
    """"Pyro Gateway (Stanton)" -> "pyro gateway"."""
    name = name.strip().lower()
    return re.sub(rf"\s*\({re.escape(system.lower())}( system)?\)$", "", name) if system else name


def place_index(terminals: dict[int, Terminal]) -> dict[str, list[Terminal]]:
    index: dict[str, list[Terminal]] = {}
    for t in terminals.values():
        for key in {_plain(t.place, t.system), _plain(t.label, t.system)} - {""}:
            index.setdefault(key, []).append(t)
    return index


def side_price(p: Price | None, side: str) -> float:
    if p is None:
        return 0.0
    return p.price_buy if side == "buy" else p.price_sell


def match_terminal(location: str, commodity_id: int, side: str, price: float, index: dict[str, list[Terminal]],
                   uex: dict[tuple[int, int], Price], systems: set[str], traded: Counter) -> Terminal | None:
    """The terminal a report came from. "stanton > arccorp > baijini point" names
    the system; "nyx gateway" doesn't, and a station can hold several terminals,
    so ties go to the terminal whose UEX price for that commodity is closest. With
    no UEX price to compare, the station's main terminal (at least twice the
    commodities of any other) takes it, never a guess between two systems."""
    parts = location.split(" > ")
    system = parts[0] if len(parts) > 1 and parts[0] in systems else None
    found = [t for t in index.get(parts[-1], []) if system is None or t.system.lower() == system]
    if len(found) <= 1:
        return found[0] if found else None
    priced = [t for t in found if side_price(uex.get((t.id, commodity_id)), side) > 0]
    if priced:
        return min(priced, key=lambda t: abs(math.log(side_price(uex[(t.id, commodity_id)], side) / price)))
    if len({t.system_id for t in found}) > 1:
        return None
    ranked = sorted(found, key=lambda t: traded[t.id], reverse=True)
    if traded[ranked[0].id] and traded[ranked[0].id] >= 2 * traded[ranked[1].id]:
        return ranked[0]
    return None


def status_from_saturation(saturation: float | None) -> int | None:
    """0 (empty) .. 1 (full) -> commodities_status 1 (out of stock) .. 7 (maximum)."""
    if saturation is None:
        return None
    return 1 + round(max(0.0, min(1.0, saturation)) * 6)


def overlays(listings: list[dict], terminals: dict[int, Terminal], commodities: dict[int, Commodity],
             prices: list[Price]) -> list[dict]:
    """Reports as overlay rows like terminal captures (source "stt"): the newest
    plausible report per terminal, commodity and side."""
    names = {c.name.lower(): c.id for c in commodities.values()}
    systems = {t.system.lower() for t in terminals.values() if t.system}
    index = place_index(terminals)
    uex = {(p.terminal_id, p.commodity_id): p for p in prices}
    traded = Counter(p.terminal_id for p in prices)    # commodities each terminal trades
    by_side: dict[tuple[int, str], list[float]] = {}
    for p in prices:
        for side in ("buy", "sell"):
            if side_price(p, side) > 0:
                by_side.setdefault((p.commodity_id, side), []).append(side_price(p, side))
    medians = {k: statistics.median(v) for k, v in by_side.items()}
    best: dict[tuple[int, int, str], dict] = {}
    for r in sorted(listings, key=lambda r: r["observed_at"], reverse=True):
        cid = names.get(r["commodity"])
        if cid is None:
            continue
        t = match_terminal(r["location"], cid, r["side"], r["price"], index, uex, systems, traded)
        if t is None or (t.id, cid, r["side"]) in best:
            continue
        ref = side_price(uex.get((t.id, cid)), r["side"]) or medians.get((cid, r["side"]))
        if not ref or not MIN_RATIO <= r["price"] / ref <= MAX_RATIO:
            continue
        best[(t.id, cid, r["side"])] = {
            "terminal_id": t.id, "commodity_id": cid, "side": r["side"], "price": r["price"],
            "scu": r["scu"] if r["side"] == "buy" else None, "status": status_from_saturation(r["saturation"]),
            "boxes": r["boxes"], "captured_at": r["observed_at"], "source": "stt"}
    return list(best.values())
