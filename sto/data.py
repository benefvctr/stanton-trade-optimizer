"""Keeps the SQLite cache current from UEX and turns it into a Dataset.

Prices refresh every few minutes, fuel and SC Trade Tools reports hourly,
everything static once a day. A failed fetch never throws out cached data: the
app keeps working from the cache and reports "stale since" the last good fetch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .models import Dataset, build_dataset, pair_key
from .sctrade import SctradeError, fetch_listings
from .store import Store
from .uex import Transport, UexClient, UexError

STATIC_ENDPOINTS = ("star_systems", "terminals", "commodities", "vehicles", "space_stations", "outposts",
                    "cities", "poi", "commodities_status")
PRICES = "commodities_prices_all"
FUEL = "fuel_prices_all"
DEFAULT_LIVE_SYSTEMS = (68, 64, 55)   # Stanton, Pyro, Nyx, until star_systems is cached
RETRY_AFTER_FAILURE_S = 60
SCTRADE = "sctrade"                    # endpoint-table key for SC Trade Tools fetch outcomes
SCTRADE_RETRY_S = 900                  # someone else's free service: back off 15 min after a failure


def orbit_key(system_id: int) -> str:
    return f"orbits_distances?id_star_system={system_id}"


@dataclass(frozen=True)
class Freshness:
    prices_at: float | None       # last good price fetch
    stale_since: float | None     # set when the latest price fetch failed
    last_error: str | None
    next_refresh_at: float


class DataService:
    def __init__(self, client: UexClient, store: Store, price_refresh_s: float = 300,
                 fuel_refresh_s: float = 3600, static_refresh_h: float = 24,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 sctrade_transport: Transport | None = None, sctrade_refresh_s: float = 3600,
                 sctrade_max_age_h: float = 168):
        self.client = client
        self.store = store
        self.price_refresh_s = price_refresh_s
        self.fuel_refresh_s = fuel_refresh_s
        self.static_refresh_s = static_refresh_h * 3600
        self.clock = clock
        self.sleep = sleep
        self.sctrade_transport = sctrade_transport     # None: SC Trade Tools reports are off
        self.sctrade_refresh_s = sctrade_refresh_s
        self.sctrade_max_age_s = sctrade_max_age_h * 3600

    # fetching

    def _due(self, key: str, max_age: float, retry_s: float = RETRY_AFTER_FAILURE_S) -> bool:
        meta = self.store.meta(key)
        now = self.clock()
        if meta["last_error"] and meta["last_attempt"] and now - meta["last_attempt"] < retry_s:
            return False
        return meta["fetched_at"] is None or now - meta["fetched_at"] >= max_age

    def _fetch(self, key: str, resource: str, params: dict | None = None) -> str | None:
        """Fetch and cache one endpoint. Returns an error message, or None on success."""
        try:
            data = self.client.get(resource, params)
        except UexError as e:
            self.store.mark_failure(key, str(e), self.clock())
            return str(e)
        self.store.save(key, data, self.clock())
        return None

    def live_system_ids(self) -> list[int]:
        systems, _ = self.store.load("star_systems")
        if not systems:
            return list(DEFAULT_LIVE_SYSTEMS)
        return [s["id"] for s in systems if s.get("is_available_live")] or list(DEFAULT_LIVE_SYSTEMS)

    def refresh(self, force: bool = False, force_prices: bool = False) -> list[str]:
        """Fetch whatever is due: everything when forced, prices and fuel when
        force_prices. Returns error messages; self.fetched lists what succeeded."""
        errors: list[str] = []
        self.fetched: list[str] = []

        def run(key: str, resource: str, params: dict | None = None) -> None:
            err = self._fetch(key, resource, params)
            if err:
                errors.append(err)
            else:
                self.fetched.append(key)

        for name in STATIC_ENDPOINTS:
            if force or self._due(name, self.static_refresh_s):
                run(name, name)
        for sid in self.live_system_ids():
            key = orbit_key(sid)
            if force or self._due(key, self.static_refresh_s):
                run(key, "orbits_distances", {"id_star_system": sid})
        if force or force_prices or self._due(PRICES, self.price_refresh_s):
            run(PRICES, PRICES)
        if force or force_prices or self._due(FUEL, self.fuel_refresh_s):
            run(FUEL, FUEL)
        if self.sctrade_transport and (force or self._due(SCTRADE, self.sctrade_refresh_s, SCTRADE_RETRY_S)):
            if self.refresh_sctrade():
                self.fetched.append(SCTRADE)
        return errors

    def refresh_sctrade(self) -> int:
        """Pull new SC Trade Tools reports. Its failures stay out of the UEX
        error list: the header shows them, and UEX prices carry on alone."""
        now = self.clock()
        oldest = now - self.sctrade_max_age_s
        try:
            rows = fetch_listings(self.store.newest_listing(), oldest, transport=self.sctrade_transport,
                                  sleep=self.sleep)
        except SctradeError as e:
            self.store.mark_failure(SCTRADE, str(e), now)
            return 0
        self.store.save_listings(rows, older_than=oldest)
        self.store.save(SCTRADE, {"new": len(rows)}, now)
        return len(rows)

    def sctrade_status(self) -> dict | None:
        """None when off; else the last fetch outcome (fetched_at, last_attempt, last_error)."""
        return self.store.meta(SCTRADE) if self.sctrade_transport else None

    def lookup_distances(self, pairs: list[tuple[int, int]], limit: int = 20, pace_s: float = 3.0) -> int:
        """Look up terminal-to-terminal distances one pair per call (UEX has no bulk
        form), skipping pairs already cached. Paced at ~20/min. Returns how many it saved."""
        cached = self.store.terminal_distances(newer_than=self.clock() - self.static_refresh_s)
        todo = [p for p in dict.fromkeys(pair_key(a, b) for a, b in pairs) if p not in cached][:limit]
        saved = 0
        for i, (a, b) in enumerate(todo):
            if i:
                self.sleep(pace_s)
            try:
                data = self.client.get("terminals_distances", {"id_terminal_origin": a, "id_terminal_destination": b})
                gm = float(data["distance"])
            except (UexError, KeyError, TypeError, ValueError):
                break
            self.store.save_terminal_distance(a, b, gm, self.clock())
            saved += 1
        return saved

    # reading

    def raw(self) -> dict | None:
        """Cached responses keyed like fixtures/snapshot.json, or None before the
        first successful price and terminal fetch."""
        out: dict = {}
        for name in (*STATIC_ENDPOINTS, PRICES, FUEL):
            data, _ = self.store.load(name)
            out[name] = data if data is not None else []
        out["orbits_distances"] = []
        for sid in self.live_system_ids():
            data, _ = self.store.load(orbit_key(sid))
            out["orbits_distances"] += data or []
        if not out["terminals"] or not out[PRICES]:
            return None
        return out

    def dataset(self, pad_overrides: dict[str, str] | None = None) -> Dataset | None:
        raw = self.raw()
        if raw is None:
            return None
        terminal_gm = self.store.terminal_distances(newer_than=self.clock() - self.static_refresh_s)
        listings = self.store.listings(newer_than=self.clock() - self.sctrade_max_age_s)             if self.sctrade_transport else None
        return build_dataset(raw, terminal_gm, pad_overrides, self.store.captures(), listings)

    def freshness(self) -> Freshness:
        meta = self.store.meta(PRICES)
        fetched, attempt, error = meta["fetched_at"], meta["last_attempt"], meta["last_error"]
        failed_last = bool(error) and attempt is not None and (fetched is None or attempt >= fetched)
        base = fetched if fetched is not None else self.clock()
        next_at = base + self.price_refresh_s
        if failed_last and attempt is not None:
            next_at = max(next_at, attempt + RETRY_AFTER_FAILURE_S)
        return Freshness(
            prices_at=fetched,
            stale_since=fetched if failed_last and fetched is not None else None,
            last_error=error if failed_last else None,
            next_refresh_at=next_at,
        )

    def cargo_ships(self) -> list[dict]:
        """Flyable ships with a cargo hold, biggest first. Concept ships are left
        out: they can't fly to a terminal yet."""
        data, _ = self.store.load("vehicles")
        ships = [v for v in data or [] if int(v.get("scu") or 0) > 0 and v.get("is_spaceship")
                 and not v.get("is_concept")]
        return sorted(ships, key=lambda v: (-int(v["scu"]), str(v.get("name_full", ""))))

    def refresh_vehicles(self) -> str | None:
        """Re-pull the ship list now (it also refreshes itself once a day)."""
        return self._fetch("vehicles", "vehicles")

    def vehicles(self, query: str = "") -> list[dict]:
        data, _ = self.store.load("vehicles")
        q = query.lower().strip()
        rows = data or []
        exact = [v for v in rows if q in (str(v.get("name_full", "")).lower(), str(v.get("name", "")).lower())]
        return exact or [v for v in rows if q in str(v.get("name_full", "")).lower()]
