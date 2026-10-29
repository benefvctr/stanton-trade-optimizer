"""Rank commodity runs. Pure functions: no I/O, "now" comes in through Settings.

For each (commodity, buy terminal, sell terminal) in one system:
    budget = wallet - reserve
    qty    = min(cargo_scu, floor(budget / buy_price), stock_at_buy, demand_at_sell)
    profit = qty * (sell_price - buy_price) - estimated_fuel_cost
    roi    = profit / (qty * buy_price)
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from .config import ShipProfile
from .distance import Leg, eta_minutes, fuel_cost, leg_distance
from .models import Commodity, Dataset, Price, Terminal

PAD_ORDER = {"XS": 0, "S": 1, "M": 2, "L": 3, "XL": 4}
FULL = 7          # commodities_status "Maximum Inventory (Full)"
STALE_BELOW = 0.25

OK, NO, UNKNOWN = "ok", "no", "unknown"


@dataclass(frozen=True)
class Settings:
    wallet: float
    reserve: float
    ship: ShipProfile
    now: float
    system_ids: frozenset[int]
    include_illegal: bool = False
    sort: str = "score"
    top_n: int = 15
    mixed_cargo: bool = False
    overhead_min: float = 8.0
    confidence_full_h: float = 2.0
    confidence_half_life_h: float = 22.0
    unknown_demand_factor: float = 0.8
    same_orbit_gm: float = 1.0
    show_unknown_pad: bool = True
    include_offline: bool = False

    @property
    def budget(self) -> float:
        return self.wallet - self.reserve


@dataclass(frozen=True)
class Cargo:
    commodity: Commodity
    buy: Price
    sell: Price
    qty: int
    stock: int
    demand: int | None

    @property
    def investment(self) -> float:
        return self.qty * self.buy.price_buy

    @property
    def gross(self) -> float:
        return self.qty * (self.sell.price_sell - self.buy.price_buy)


@dataclass(frozen=True)
class Route:
    buy_terminal: Terminal
    sell_terminal: Terminal
    cargo: tuple[Cargo, ...]
    leg: Leg
    eta_min: float
    fuel_cost: float
    profit: float
    buy_age_h: float
    sell_age_h: float
    confidence: float
    flags: tuple[str, ...]

    @property
    def commodity(self) -> Commodity:
        return self.cargo[0].commodity

    @property
    def qty(self) -> int:
        return sum(c.qty for c in self.cargo)

    @property
    def investment(self) -> float:
        return sum(c.investment for c in self.cargo)

    @property
    def roi(self) -> float:
        return self.profit / self.investment if self.investment else 0.0

    @property
    def margin_pct(self) -> float:
        """Primary commodity's unit margin over its sell price, as UEX computes it."""
        c = self.cargo[0]
        return (c.sell.price_sell - c.buy.price_buy) / c.sell.price_sell * 100

    @property
    def profit_per_hour(self) -> float:
        return self.profit / (self.eta_min / 60) if self.eta_min > 0 else 0.0

    @property
    def score(self) -> float:
        return self.profit * self.confidence

    @property
    def age_h(self) -> float:
        return max(self.buy_age_h, self.sell_age_h)


def confidence(age_h: float, full_h: float, half_life_h: float) -> float:
    if age_h <= full_h:
        return 1.0
    return 0.5 ** ((age_h - full_h) / half_life_h)


def pad_check(t: Terminal, ship_pad: str) -> str:
    if t.pad_types is None:
        return UNKNOWN
    need = PAD_ORDER.get(ship_pad.upper())
    if need is None:
        return UNKNOWN
    return OK if any(PAD_ORDER.get(p, -1) >= need for p in t.pad_types) else NO


def box_check(sizes: tuple[int, ...] | None, ship_max: int) -> str:
    """No when even the smallest box offered is too big for the hold's grid."""
    if not sizes:
        return UNKNOWN
    return NO if min(sizes) > ship_max else OK


def _terminal_ok(t: Terminal | None, s: Settings) -> bool:
    return t is not None and t.system_id in s.system_ids and (t.is_live or s.include_offline)


def _commodity_ok(c: Commodity | None, s: Settings) -> bool:
    return c is not None and (s.include_illegal or not c.is_illegal)


def _sort_key(sort: str):
    if sort == "profit":
        return lambda r: (r.profit, r.score)
    if sort == "profit_per_hour":
        return lambda r: (r.profit_per_hour, r.score)
    return lambda r: (r.score, r.profit)


def find_routes(ds: Dataset, s: Settings) -> list[Route]:
    if s.budget <= 0:
        return []
    ship = s.ship
    buys: dict[int, list[Price]] = defaultdict(list)
    sells: dict[int, list[Price]] = defaultdict(list)
    for p in ds.prices:
        t = ds.terminals.get(p.terminal_id)
        if not _terminal_ok(t, s) or not _commodity_ok(ds.commodities.get(p.commodity_id), s):
            continue
        if p.price_buy > 0 and p.scu_buy > 0:
            buys[p.commodity_id].append(p)
        if p.price_sell > 0 and p.status_sell != FULL:
            sells[p.commodity_id].append(p)

    pad_cache: dict[int, str] = {}

    def pad(t: Terminal) -> str:
        if t.id not in pad_cache:
            pad_cache[t.id] = pad_check(t, ship.pad)
        return pad_cache[t.id]

    # Every viable single-commodity leg, grouped by terminal pair.
    legs: dict[tuple[int, int], list[Cargo]] = defaultdict(list)
    for cid, buy_rows in buys.items():
        commodity = ds.commodities[cid]
        for b in buy_rows:
            bt = ds.terminals[b.terminal_id]
            if pad(bt) == NO or box_check(b.container_sizes, ship.max_container_scu) == NO:
                continue
            for sp in sells.get(cid, ()):
                if sp.terminal_id == b.terminal_id or sp.price_sell <= b.price_buy:
                    continue
                st = ds.terminals[sp.terminal_id]
                if st.system_id != bt.system_id or pad(st) == NO:
                    continue
                if box_check(sp.container_sizes, ship.max_container_scu) == NO:
                    continue
                if not s.show_unknown_pad and UNKNOWN in (pad(bt), pad(st)):
                    continue
                legs[(bt.id, st.id)].append(Cargo(commodity, b, sp, 0, b.scu_buy, sp.demand))

    routes: list[Route] = []
    median_cache: dict[int, float] = {}
    for (bid, sid), candidates in legs.items():
        bt, st = ds.terminals[bid], ds.terminals[sid]
        leg = leg_distance(ds, bt, st, s.same_orbit_gm, median_cache)
        fuel = fuel_cost(leg.gm, ship, ds.qt_fuel_price.get(bt.system_id, ds.qt_fuel_price.get(0, 0.0)))
        eta = eta_minutes(leg.gm, ship, s.overhead_min)
        if s.mixed_cargo:
            candidates = sorted(candidates, key=lambda c: c.sell.price_sell - c.buy.price_buy, reverse=True)
            fills = [_fill(candidates, ship.cargo_scu, s.budget)]
        else:
            fills = [_fill([c], ship.cargo_scu, s.budget) for c in candidates]
        for cargo in fills:
            if not cargo:
                continue
            route = _route(bt, st, tuple(cargo), leg, eta, fuel, s)
            if route.profit > 0:
                routes.append(route)

    routes.sort(key=_sort_key(s.sort), reverse=True)
    return routes[: s.top_n]


def _fill(candidates: list[Cargo], hold: int, budget: float) -> list[Cargo]:
    """Load candidates in order, each limited by remaining hold, remaining
    budget, its stock at the buy terminal and its demand at the sell terminal."""
    out = []
    for c in candidates:
        if hold <= 0 or budget <= 0:
            break
        limits = [hold, math.floor(budget / c.buy.price_buy), c.stock]
        if c.demand is not None:
            limits.append(c.demand)
        qty = min(limits)
        if qty <= 0:
            continue
        loaded = Cargo(c.commodity, c.buy, c.sell, qty, c.stock, c.demand)
        out.append(loaded)
        hold -= qty
        budget -= loaded.investment
    return out


def _route(bt: Terminal, st: Terminal, cargo: tuple[Cargo, ...], leg: Leg, eta: float, fuel: float,
           s: Settings) -> Route:
    buy_age = max((s.now - c.buy.updated) / 3600 for c in cargo)
    sell_age = max((s.now - c.sell.updated) / 3600 for c in cargo)
    conf = min(confidence(buy_age, s.confidence_full_h, s.confidence_half_life_h),
               confidence(sell_age, s.confidence_full_h, s.confidence_half_life_h))
    flags = []
    if any("you" in (c.buy.source, c.sell.source) for c in cargo):
        flags.append("you")
    if any(c.demand is None for c in cargo):
        conf *= s.unknown_demand_factor
        flags.append("demand ?")
    if UNKNOWN in (pad_check(bt, s.ship.pad), pad_check(st, s.ship.pad)):
        flags.append("pad ?")
    if any(UNKNOWN in (box_check(c.buy.container_sizes, s.ship.max_container_scu),
                       box_check(c.sell.container_sizes, s.ship.max_container_scu)) for c in cargo):
        flags.append("box ?")
    if leg.is_estimate:
        flags.append("dist ~")
    if conf < STALE_BELOW:
        flags.append("stale")
    profit = sum(c.gross for c in cargo) - fuel
    return Route(bt, st, cargo, leg, eta, fuel, profit, buy_age, sell_age, conf, tuple(flags))
