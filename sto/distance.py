"""Route distance, travel time and fuel estimates.

UEX gives orbit-to-orbit distances per system in one call, and terminal pairs
one call at a time. It has no quantum drive speeds or fuel burn rates, so those
come from the ship profile in config.toml.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from .config import ShipProfile
from .models import Dataset, Terminal, pair_key

EXACT = "terminal"     # looked up with terminals_distances
ORBIT = "orbit"        # orbits_distances between the two terminals' orbits
ESTIMATE = "estimate"  # same orbit, or a missing pair: shown with "~"


@dataclass(frozen=True)
class Leg:
    gm: float
    quality: str

    @property
    def is_estimate(self) -> bool:
        return self.quality == ESTIMATE


def system_median_gm(ds: Dataset, system_id: int) -> float:
    orbits = {t.orbit_id for t in ds.terminals.values() if t.system_id == system_id}
    values = [gm for (a, b), gm in ds.orbit_gm.items() if a in orbits and b in orbits and a != b and gm > 0]
    return statistics.median(values) if values else 30.0


def cross_median_gm(ds: Dataset, a_system: int, b_system: int) -> float:
    """Median orbit-to-orbit distance between two systems; UEX's per-system
    distance lists include the pairs that cross a jump point."""
    a_orbits = {t.orbit_id for t in ds.terminals.values() if t.system_id == a_system}
    b_orbits = {t.orbit_id for t in ds.terminals.values() if t.system_id == b_system}
    values = [gm for (x, y), gm in ds.orbit_gm.items() if gm > 0 and (
        (x in a_orbits and y in b_orbits) or (x in b_orbits and y in a_orbits))]
    return statistics.median(values) if values else 100.0


def leg_distance(ds: Dataset, a: Terminal, b: Terminal, same_orbit_gm: float,
                 median_cache: dict | None = None) -> Leg:
    exact = ds.terminal_gm.get(pair_key(a.id, b.id))
    if exact is not None:
        return Leg(exact, EXACT)
    if a.system_id != b.system_id:
        gm = ds.orbit_gm.get(pair_key(a.orbit_id, b.orbit_id))
        if gm is not None and gm > 0:
            return Leg(gm, ORBIT)
        key = pair_key(a.system_id, b.system_id)
        cache = median_cache if median_cache is not None else {}
        if key not in cache:
            cache[key] = cross_median_gm(ds, *key)
        return Leg(cache[key], ESTIMATE)
    if a.orbit_id == b.orbit_id:
        return Leg(same_orbit_gm, ESTIMATE)
    gm = ds.orbit_gm.get(pair_key(a.orbit_id, b.orbit_id))
    if gm is not None and gm > 0:
        return Leg(gm, ORBIT)
    if median_cache is not None:
        if a.system_id not in median_cache:
            median_cache[a.system_id] = system_median_gm(ds, a.system_id)
        return Leg(median_cache[a.system_id], ESTIMATE)
    return Leg(system_median_gm(ds, a.system_id), ESTIMATE)


def eta_minutes(gm: float, ship: ShipProfile, overhead_min: float) -> float:
    travel_s = ship.qt_spool_s + (gm / ship.qt_speed_gm_s if ship.qt_speed_gm_s > 0 else 0)
    return overhead_min + travel_s / 60.0


def fuel_cost(gm: float, ship: ShipProfile, qt_fuel_price: float) -> float:
    return gm * ship.qt_fuel_scu_per_gm * qt_fuel_price


def missing_orbit_pairs(ds: Dataset, system_ids: set[int]) -> list[tuple[int, int]]:
    """One representative terminal pair per orbit pair that has no distance yet,
    among live terminals that trade something, in the given systems."""
    trading = {p.terminal_id for p in ds.prices}
    by_orbit: dict[int, int] = {}
    for t in sorted(ds.terminals.values(), key=lambda t: t.id):
        if t.system_id in system_ids and t.is_live and t.id in trading and t.orbit_id:
            by_orbit.setdefault(t.orbit_id, t.id)
    orbits = sorted(by_orbit)
    pairs = []
    for i, oa in enumerate(orbits):
        for ob in orbits[i + 1:]:
            if ds.terminals[by_orbit[oa]].system_id != ds.terminals[by_orbit[ob]].system_id:
                continue
            if pair_key(oa, ob) not in ds.orbit_gm:
                pairs.append((by_orbit[oa], by_orbit[ob]))
    return pairs
