"""Typed views of UEX records. Field names follow the real API responses saved in
fixtures/ (see docs/API_FIELDS.md); nothing here is guessed from memory."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field, replace

TRADE_TYPES = ("commodity", "commodity_raw")
LOCATION_TABLES = (("id_space_station", "space_stations"), ("id_outpost", "outposts"),
                   ("id_city", "cities"), ("id_poi", "poi"))
QUANTUM_FUEL = "Quantum Fuel"


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def parse_csv_sizes(value) -> tuple[int, ...] | None:
    """"1,2,4,8" -> (1, 2, 4, 8); empty or missing -> None (unknown)."""
    if not value:
        return None
    sizes = tuple(sorted({_int(v) for v in str(value).split(",") if _int(v) > 0}))
    return sizes or None


def parse_pads(value) -> frozenset[str] | None:
    """"S,M,L" -> {"S","M","L"}; empty or missing -> None (unknown)."""
    if not value:
        return None
    pads = frozenset(p.strip().upper() for p in str(value).split(",") if p.strip())
    return pads or None


def pair_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


@dataclass(frozen=True)
class Terminal:
    id: int
    name: str
    nickname: str
    code: str
    type: str
    system_id: int
    system: str
    orbit_id: int
    orbit: str
    place: str
    is_live: bool
    max_container: int | None          # terminals.max_container_size; 0 means unknown
    pad_types: frozenset[str] | None   # from the location's pad_types; None means unknown
    pad_source: str                    # "uex", "override" or ""
    has_docking_port: bool
    has_loading_dock: bool
    has_freight_elevator: bool
    is_auto_load: bool
    faction: str

    @property
    def label(self) -> str:
        """Short name for the table; drops a redundant "(Stanton)" style suffix."""
        name = self.nickname or self.name
        for suffix in (f" ({self.system} system)", f" ({self.system})"):
            if self.system and name.endswith(suffix):
                return name[: -len(suffix)]
        return name


@dataclass(frozen=True)
class Commodity:
    id: int
    name: str
    code: str
    kind: str
    is_illegal: bool


@dataclass(frozen=True)
class Price:
    commodity_id: int
    terminal_id: int
    price_buy: float
    price_sell: float
    scu_buy: int
    scu_buy_avg: int
    scu_sell: int          # "scu sell demand (forecasted)"; 0 when nobody reported it
    scu_sell_avg: int
    scu_sell_stock: int    # the terminal's own inventory, not its demand
    status_buy: int        # commodities_status code, 1 = out of stock ... 7 = maximum
    status_sell: int
    container_sizes: tuple[int, ...] | None
    updated: float         # date_modified, unix seconds
    source: str = "uex"    # "uex", "you" (read off an in-game terminal) or "stt" (SC Trade Tools report)

    @property
    def demand(self) -> int | None:
        """SCU the sell terminal will take: last report, then average, else unknown."""
        if self.scu_sell > 0:
            return self.scu_sell
        if self.scu_sell_avg > 0:
            return self.scu_sell_avg
        return None


@dataclass
class Dataset:
    systems: dict[int, str]
    terminals: dict[int, Terminal]
    commodities: dict[int, Commodity]
    prices: list[Price]
    orbit_gm: dict[tuple[int, int], float]
    terminal_gm: dict[tuple[int, int], float]
    qt_fuel_price: dict[int, float]                   # system id -> median aUEC per SCU
    status_names: dict[str, dict[int, str]] = field(default_factory=dict)
    # system id -> {connected system id: name of the gateway station in the first system}
    jumps: dict[int, dict[int, str]] = field(default_factory=dict)
    sctrade_used: int = 0                             # SC Trade Tools reports newer than UEX

    def system_id(self, name: str) -> int | None:
        return next((i for i, n in self.systems.items() if n.lower() == name.lower()), None)


def _place(t: dict) -> str:
    for key in ("space_station_name", "outpost_name", "city_name", "moon_name", "planet_name", "orbit_name"):
        if t.get(key):
            return t[key]
    return ""


def build_terminals(rows: list[dict], locations: dict[str, list[dict]],
                    overrides: dict[str, str] | None = None) -> dict[int, Terminal]:
    """Trade terminals only. Pad sizes come from the terminal's station, outpost,
    city or POI record, then from config [pad_overrides] by nickname or code."""
    by_table = {name: {r["id"]: r for r in locations.get(name, [])} for _, name in LOCATION_TABLES}
    over = {k.lower(): v for k, v in (overrides or {}).items()}
    out: dict[int, Terminal] = {}
    for t in rows:
        if t.get("type") not in TRADE_TYPES:
            continue
        pads, source = None, ""
        for key, table in LOCATION_TABLES:
            if t.get(key):
                loc = by_table[table].get(t[key])
                pads = parse_pads(loc.get("pad_types")) if loc else None
                break
        if pads is not None:
            source = "uex"
        for k in (str(t.get("nickname") or "").lower(), str(t.get("code") or "").lower()):
            if k and k in over:
                pads, source = frozenset(p.strip().upper() for p in over[k].split(",") if p.strip()), "override"
                break
        out[t["id"]] = Terminal(
            id=t["id"], name=t.get("name") or "", nickname=t.get("nickname") or "", code=t.get("code") or "",
            type=t["type"], system_id=_int(t.get("id_star_system")), system=t.get("star_system_name") or "",
            orbit_id=_int(t.get("id_orbit")), orbit=t.get("orbit_name") or "", place=_place(t),
            is_live=bool(t.get("is_available_live")),
            max_container=_int(t.get("max_container_size")) or None,
            pad_types=pads, pad_source=source,
            has_docking_port=bool(t.get("has_docking_port")), has_loading_dock=bool(t.get("has_loading_dock")),
            has_freight_elevator=bool(t.get("has_freight_elevator")), is_auto_load=bool(t.get("is_auto_load")),
            faction=t.get("faction_name") or "",
        )
    return out


def build_prices(rows: list[dict]) -> list[Price]:
    return [
        Price(
            commodity_id=p["id_commodity"], terminal_id=p["id_terminal"],
            price_buy=float(p.get("price_buy") or 0), price_sell=float(p.get("price_sell") or 0),
            scu_buy=_int(p.get("scu_buy")), scu_buy_avg=_int(p.get("scu_buy_avg")),
            scu_sell=_int(p.get("scu_sell")), scu_sell_avg=_int(p.get("scu_sell_avg")),
            scu_sell_stock=_int(p.get("scu_sell_stock")),
            status_buy=_int(p.get("status_buy")), status_sell=_int(p.get("status_sell")),
            container_sizes=parse_csv_sizes(p.get("container_sizes")),
            updated=float(p.get("date_modified") or 0),
        )
        for p in rows
    ]


def apply_captures(prices: list[Price], captures: list[dict]) -> list[Price]:
    """Overlay newer prices: ones read off in-game terminals (source "you") and
    SC Trade Tools reports (source "stt"). Per terminal, commodity and side the
    newest report wins, yours on a tie. It replaces that side of the UEX row
    (buy or sell) unless UEX has a newer report; the other side stays as UEX had
    it, with its own age."""
    out = list(prices)
    index = {(p.commodity_id, p.terminal_id): i for i, p in enumerate(out)}
    newest: dict[tuple, tuple] = {}
    for c in captures:
        key = (c["terminal_id"], c["commodity_id"], c["side"])
        rank = (c["captured_at"], c.get("source", "you") == "you")
        if c.get("price") and (key not in newest or rank > newest[key][0]):
            newest[key] = (rank, c)
    for _, c in sorted(newest.values(), key=lambda v: v[0]):
        source = c.get("source", "you")
        i = index.get((c["commodity_id"], c["terminal_id"]))
        uex = out[i] if i is not None else None
        if uex is not None and uex.updated >= c["captured_at"]:
            continue
        if c["side"] == "buy":
            stock = c["scu"] if c.get("scu") is not None else (uex.scu_buy if uex else 0)
            new = Price(c["commodity_id"], c["terminal_id"], price_buy=float(c["price"]), price_sell=0,
                        scu_buy=int(stock), scu_buy_avg=int(stock), scu_sell=0, scu_sell_avg=0, scu_sell_stock=0,
                        status_buy=int(c.get("status") or (uex.status_buy if uex else 0)), status_sell=0,
                        container_sizes=parse_csv_sizes(c.get("boxes")) or (uex.container_sizes if uex else None),
                        updated=float(c["captured_at"]), source=source)
            rest = replace(uex, price_buy=0, scu_buy=0, scu_buy_avg=0, status_buy=0) if uex else None
        else:
            # A terminal you read listed it under IN DEMAND, so it isn't full: no status 7 from a stale
            # report. An SC Trade Tools report says how full the shop was (7 when it was full).
            new = Price(c["commodity_id"], c["terminal_id"], price_buy=0, price_sell=float(c["price"]),
                        scu_buy=0, scu_buy_avg=0,
                        scu_sell=uex.scu_sell if uex else 0, scu_sell_avg=uex.scu_sell_avg if uex else 0,
                        scu_sell_stock=uex.scu_sell_stock if uex else 0, status_buy=0,
                        status_sell=int(c.get("status") or 0),
                        container_sizes=parse_csv_sizes(c.get("boxes")) or (uex.container_sizes if uex else None),
                        updated=float(c["captured_at"]), source=source)
            rest = replace(uex, price_sell=0, scu_sell=0, scu_sell_avg=0, status_sell=0) if uex else None
        if rest is not None:
            out[i] = rest
        out.append(new)
    return out


def build_commodities(rows: list[dict]) -> dict[int, Commodity]:
    return {c["id"]: Commodity(c["id"], c.get("name") or "", c.get("code") or "", c.get("kind") or "",
                               bool(c.get("is_illegal"))) for c in rows}


def build_orbit_distances(rows: list[dict]) -> dict[tuple[int, int], float]:
    out: dict[tuple[int, int], float] = {}
    for o in rows:
        try:
            gm = float(o["distance"])
        except (KeyError, TypeError, ValueError):
            continue
        out[pair_key(_int(o["id_orbit_origin"]), _int(o["id_orbit_destination"]))] = gm
    return out


def build_fuel_prices(rows: list[dict], terminal_system: dict[int, int]) -> dict[int, float]:
    """Median Quantum Fuel price per system; key 0 holds the all-systems median."""
    by_system: dict[int, list[float]] = {}
    for f in rows:
        if f.get("commodity_name") != QUANTUM_FUEL or not f.get("price_buy"):
            continue
        price = float(f["price_buy"])
        by_system.setdefault(0, []).append(price)
        sid = terminal_system.get(f["id_terminal"])
        if sid:
            by_system.setdefault(sid, []).append(price)
    return {sid: statistics.median(v) for sid, v in by_system.items()}


def build_status_names(status: dict | None) -> dict[str, dict[int, str]]:
    if not isinstance(status, dict):
        return {}
    # "Out of Stock (Empty)" -> "Out of Stock"
    return {side: {_int(s["code"]): re.sub(r"\s*\(.*\)$", "", s.get("name") or s.get("name_short") or "")
                   for s in rows}
            for side, rows in status.items() if isinstance(rows, list)}


GATEWAY = re.compile(r"^(?P<to>.+?) Gateway \((?P<at>.+)\)$")


def build_jumps(terminals: dict[int, Terminal], systems: dict[int, str]) -> dict[int, dict[int, str]]:
    """Which live systems connect, read off gateway stations: "Pyro Gateway
    (Stanton)" is in Stanton and jumps to Pyro."""
    ids = {name.lower(): sid for sid, name in systems.items()}
    out: dict[int, dict[int, str]] = {}
    for t in terminals.values():
        m = GATEWAY.match(t.place)
        to = ids.get(m["to"].lower()) if m else None
        if to is not None and t.system_id in systems and to != t.system_id:
            out.setdefault(t.system_id, {})[to] = f"{m['to']} Gateway"
    return out


def build_dataset(raw: dict, terminal_gm: dict[tuple[int, int], float] | None = None,
                  pad_overrides: dict[str, str] | None = None, captures: list[dict] | None = None,
                  listings: list[dict] | None = None) -> Dataset:
    """raw maps endpoint names (star_systems, terminals, commodities_prices_all,
    orbits_distances, ...) to their response data, as in fixtures/snapshot.json.
    captures are your terminal reads, listings SC Trade Tools reports."""
    systems = {s["id"]: s["name"] for s in raw.get("star_systems", []) if s.get("is_available_live")}
    terminals = build_terminals(raw.get("terminals", []), {n: raw.get(n, []) for _, n in LOCATION_TABLES}, pad_overrides)
    terminal_system = {t["id"]: _int(t.get("id_star_system")) for t in raw.get("terminals", [])}
    orbit_gm = build_orbit_distances(raw.get("orbits_distances", []))
    terminal_gm = dict(terminal_gm or {})
    # A looked-up terminal distance also fills a gap in the orbit table, since
    # gap fills fetch one representative terminal pair per missing orbit pair.
    for (a, b), gm in terminal_gm.items():
        ta, tb = terminals.get(a), terminals.get(b)
        if ta and tb and ta.orbit_id != tb.orbit_id:
            orbit_gm.setdefault(pair_key(ta.orbit_id, tb.orbit_id), gm)
    commodities = build_commodities(raw.get("commodities", []))
    prices = build_prices(raw.get("commodities_prices_all", []))
    reports = []
    if listings:
        from .sctrade import overlays

        reports = overlays(listings, terminals, commodities, prices)
    prices = apply_captures(prices, [*(captures or []), *reports])
    return Dataset(
        systems=systems,
        terminals=terminals,
        commodities=commodities,
        prices=prices,
        orbit_gm=orbit_gm,
        terminal_gm=terminal_gm,
        qt_fuel_price=build_fuel_prices(raw.get("fuel_prices_all", []), terminal_system),
        status_names=build_status_names(raw.get("commodities_status")),
        jumps=build_jumps(terminals, systems),
        sctrade_used=sum(1 for p in prices if p.source == "stt"),
    )
