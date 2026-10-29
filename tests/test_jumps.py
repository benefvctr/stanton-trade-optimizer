"""Runs that cross a jump point: which systems connect, when crossing pairs are
allowed, the distance and the jump minutes, on a hand-built pair of systems and
on the real snapshot."""
from dataclasses import replace

import pytest

from sto.distance import ESTIMATE, ORBIT, leg_distance
from sto.models import build_jumps
from sto.optimizer import find_routes

from test_optimizer import buy, dataset, sell, settings, terminal

STANTON, PYRO = 68, 64


def two_systems(orbit_gm=None):
    """Buy in Stanton (orbit 10), sell in Pyro (orbit 30), a gateway on each side."""
    ts = [terminal(1, 10), terminal(2, 30, system=PYRO),
          replace(terminal(3, 11), place="Pyro Gateway (Stanton)"),
          replace(terminal(4, 31, system=PYRO), place="Stanton Gateway (Pyro)")]
    ds = dataset([buy(1, 1, 100.0), sell(1, 2, 150.0, demand=1000)], terminals=ts,
                 orbit_gm=orbit_gm if orbit_gm is not None else {(10, 30): 80.0})
    ds.jumps = build_jumps(ds.terminals, ds.systems)
    return ds


def test_gateway_stations_say_which_systems_connect(snapshot_ds):
    assert build_jumps(two_systems().terminals, {68: "Stanton", 64: "Pyro"}) == {
        STANTON: {PYRO: "Pyro Gateway"}, PYRO: {STANTON: "Stanton Gateway"}}
    names = {snapshot_ds.systems[a]: {snapshot_ds.systems[b]: g for b, g in m.items()}
             for a, m in snapshot_ds.jumps.items()}
    assert names == {"Stanton": {"Pyro": "Pyro Gateway", "Nyx": "Nyx Gateway"},
                     "Pyro": {"Stanton": "Stanton Gateway", "Nyx": "Nyx Gateway"},
                     "Nyx": {"Pyro": "Pyro Gateway", "Stanton": "Stanton Gateway"}}   # Terra isn't live


def test_crossing_runs_need_both_systems_and_the_switch(centaurus):
    ds = two_systems()
    both = frozenset({STANTON, PYRO})
    assert find_routes(ds, settings(centaurus, system_ids=both)) == []                   # switch off
    assert find_routes(ds, settings(centaurus, cross_system=True)) == []                 # Pyro not in scope
    [r] = find_routes(ds, settings(centaurus, system_ids=both, cross_system=True))
    assert (r.buy_terminal.system, r.sell_terminal.system, r.via) == ("Stanton", "Pyro", "Pyro Gateway")


def test_unconnected_systems_never_pair(centaurus):
    ds = two_systems()
    ds.jumps = {}
    assert find_routes(ds, settings(centaurus, system_ids=frozenset({STANTON, PYRO}), cross_system=True)) == []


def test_a_jump_adds_its_minutes_and_uses_the_cross_system_distance(centaurus):
    ds = two_systems()
    [r] = find_routes(ds, settings(centaurus, system_ids=frozenset({STANTON, PYRO}), cross_system=True, jump_min=7))
    assert r.leg.gm == 80.0 and r.leg.quality == ORBIT
    assert r.eta_min == pytest.approx(8 + (10 + 80 / 0.2) / 60 + 7)
    assert r.fuel_cost == pytest.approx(80 * 0.02 * 1000)


def test_a_missing_cross_pair_is_estimated_from_that_pair_of_systems():
    ds = two_systems(orbit_gm={(11, 31): 60.0, (10, 31): 100.0, (10, 11): 5.0})
    leg = leg_distance(ds, ds.terminals[1], ds.terminals[2], 1.0, {})
    assert leg.quality == ESTIMATE and leg.gm == 80.0          # median of 60 and 100; the in-system 5 is left out


def test_the_snapshot_has_crossing_runs_with_real_distances(snapshot_ds, snapshot_now, centaurus):
    s = settings(centaurus, wallet=1_500_000, now=snapshot_now, system_ids=frozenset(snapshot_ds.systems),
                 cross_system=True, top_n=50)
    crossing = [r for r in find_routes(snapshot_ds, s) if r.via]
    assert crossing
    for r in crossing:
        assert r.buy_terminal.system != r.sell_terminal.system
        assert r.via == snapshot_ds.jumps[r.buy_terminal.system_id][r.sell_terminal.system_id]
    assert any(r.leg.quality == ORBIT for r in crossing)
