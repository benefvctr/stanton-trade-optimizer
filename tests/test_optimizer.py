"""Optimizer rules, on a small hand-built dataset (exact numbers) and on the real
UEX snapshot in fixtures/snapshot.json (properties that must hold everywhere)."""
from dataclasses import replace

import pytest

from sto.models import Commodity, Dataset, Price, Terminal
from sto.optimizer import Settings, confidence, find_routes

NOW = 1_800_000_000.0
HOUR = 3600.0
STANTON = 68


def terminal(tid, orbit, pads=frozenset({"S", "M", "L"}), system=STANTON, live=True, **kw):
    return Terminal(
        id=tid, name=f"T{tid}", nickname=f"T{tid}", code=f"T{tid}", type="commodity",
        system_id=system, system="Stanton" if system == STANTON else "Pyro", orbit_id=orbit, orbit=f"O{orbit}",
        place=f"P{tid}", is_live=live, max_container=32, pad_types=pads, pad_source="uex" if pads is not None else "",
        has_docking_port=False, has_loading_dock=False, has_freight_elevator=True, is_auto_load=False, faction="",
        **kw,
    )


def buy(cid, tid, price, stock=1000, age_h=1.0, sizes=(1, 2, 4, 8, 16, 24, 32), status=7):
    return Price(cid, tid, price_buy=price, price_sell=0, scu_buy=stock, scu_buy_avg=stock, scu_sell=0,
                 scu_sell_avg=0, scu_sell_stock=0, status_buy=status, status_sell=0, container_sizes=sizes,
                 updated=NOW - age_h * HOUR)


def sell(cid, tid, price, demand=0, age_h=1.0, sizes=(1, 2, 4, 8, 16, 24, 32), status=1):
    return Price(cid, tid, price_buy=0, price_sell=price, scu_buy=0, scu_buy_avg=0, scu_sell=demand,
                 scu_sell_avg=0, scu_sell_stock=0, status_buy=0, status_sell=status, container_sizes=sizes,
                 updated=NOW - age_h * HOUR)


def dataset(prices, terminals=None, commodities=None, orbit_gm=None):
    terminals = terminals or [terminal(1, 10), terminal(2, 20)]
    commodities = commodities or [Commodity(1, "Gold", "GOLD", "Metal", False),
                                  Commodity(2, "Tin", "TIN", "Metal", False),
                                  Commodity(3, "WiDoW", "WIDO", "Drug", True)]
    return Dataset(
        systems={68: "Stanton", 64: "Pyro"},
        terminals={t.id: t for t in terminals},
        commodities={c.id: c for c in commodities},
        prices=prices,
        orbit_gm=orbit_gm if orbit_gm is not None else {(10, 20): 10.0},
        terminal_gm={},
        qt_fuel_price={0: 1000.0, 68: 1000.0, 64: 1000.0},
    )


def settings(ship, wallet=1_150_000, **kw):
    base = dict(wallet=wallet, reserve=150_000, ship=ship, now=NOW, system_ids=frozenset({STANTON}))
    base.update(kw)
    return Settings(**base)


# --- exact numbers -------------------------------------------------------------

def test_profit_roi_eta_and_fuel_follow_the_formula(centaurus):
    ds = dataset([buy(1, 1, 100.0), sell(1, 2, 150.0, demand=1000)])
    [r] = find_routes(ds, settings(centaurus))
    assert r.qty == 240                                    # hold is the limit: budget allows 10,000
    assert r.investment == 24_000
    assert r.fuel_cost == pytest.approx(10 * 0.02 * 1000)  # 10 Gm x burn x fuel price
    assert r.profit == pytest.approx(240 * 50 - 200)
    assert r.roi == pytest.approx(r.profit / 24_000)
    assert r.margin_pct == pytest.approx(50 / 150 * 100)
    assert r.eta_min == pytest.approx(8 + (10 + 10 / 0.2) / 60)
    assert r.profit_per_hour == pytest.approx(r.profit / (r.eta_min / 60))
    assert r.confidence == 1.0
    assert r.flags == ()


def test_budget_limits_qty_and_is_never_exceeded(centaurus):
    ds = dataset([buy(1, 1, 10_000.0), sell(1, 2, 12_000.0, demand=500)])
    [r] = find_routes(ds, settings(centaurus, wallet=150_000 + 1_234_567))
    assert r.qty == 123                                    # floor(1,234,567 / 10,000)
    assert r.investment <= 1_234_567


def test_budget_smaller_than_one_scu_gives_no_route(centaurus):
    ds = dataset([buy(1, 1, 10_000.0), sell(1, 2, 12_000.0)])
    assert find_routes(ds, settings(centaurus, wallet=150_000 + 9_999)) == []
    assert find_routes(ds, settings(centaurus, wallet=100_000)) == []   # wallet under the reserve


def test_zero_stock_at_buy_is_excluded(centaurus):
    ds = dataset([buy(1, 1, 100.0, stock=0), sell(1, 2, 150.0)])
    assert find_routes(ds, settings(centaurus)) == []
    ds = dataset([buy(1, 1, 100.0, stock=37), sell(1, 2, 150.0)])
    assert find_routes(ds, settings(centaurus))[0].qty == 37


def test_demand_smaller_than_qty_caps_qty(centaurus):
    ds = dataset([buy(1, 1, 100.0), sell(1, 2, 150.0, demand=30)])
    [r] = find_routes(ds, settings(centaurus))
    assert r.qty == 30
    assert "demand ?" not in r.flags
    assert r.confidence == 1.0


def test_unknown_demand_is_flagged_and_discounted(centaurus):
    ds = dataset([buy(1, 1, 100.0), sell(1, 2, 150.0, demand=0)])
    [r] = find_routes(ds, settings(centaurus))
    assert r.qty == 240
    assert "demand ?" in r.flags
    assert r.confidence == pytest.approx(0.8)


def test_full_sell_terminal_is_excluded(centaurus):
    ds = dataset([buy(1, 1, 100.0), sell(1, 2, 150.0, status=7)])
    assert find_routes(ds, settings(centaurus)) == []


def test_no_margin_is_excluded(centaurus):
    ds = dataset([buy(1, 1, 150.0), sell(1, 2, 150.0)])
    assert find_routes(ds, settings(centaurus)) == []


# --- data age ------------------------------------------------------------------

def test_confidence_curve():
    assert confidence(0.5, 2, 22) == 1.0
    assert confidence(2, 2, 22) == 1.0
    assert confidence(24, 2, 22) == pytest.approx(0.5)
    assert confidence(72, 2, 22) == pytest.approx(0.5 ** (70 / 22))
    assert confidence(72, 2, 22) < 0.12
    assert confidence(-1, 2, 22) == 1.0                    # clock skew never boosts above 1


def test_stale_data_ranks_below_fresh_by_score_but_not_by_profit(centaurus):
    t = [terminal(1, 10), terminal(2, 20), terminal(3, 30)]
    ds = dataset(
        [buy(1, 1, 100.0, age_h=1), sell(1, 2, 150.0, demand=240, age_h=1),        # fresh, +50/SCU
         buy(2, 1, 100.0, age_h=1), sell(2, 3, 170.0, demand=240, age_h=72)],      # stale sell side, +70/SCU
        terminals=t, orbit_gm={(10, 20): 10.0, (10, 30): 10.0, (20, 30): 10.0},
    )
    by_score = find_routes(ds, settings(centaurus, sort="score"))
    assert [r.commodity.name for r in by_score] == ["Gold", "Tin"]
    stale = by_score[1]
    assert stale.confidence == pytest.approx(confidence(72, 2, 22))
    assert stale.sell_age_h == pytest.approx(72)
    assert "stale" in stale.flags
    by_profit = find_routes(ds, settings(centaurus, sort="profit"))
    assert [r.commodity.name for r in by_profit] == ["Tin", "Gold"]


# --- ship compatibility --------------------------------------------------------

def test_container_size_mismatch_is_excluded(centaurus):
    small_boxes_only = replace(centaurus, max_container_scu=4)
    ds = dataset([buy(1, 1, 100.0, sizes=(8, 16, 24, 32)), sell(1, 2, 150.0)])
    assert find_routes(ds, settings(small_boxes_only)) == []
    assert len(find_routes(ds, settings(centaurus))) == 1


def test_terminal_with_smaller_boxes_than_the_ship_is_fine(centaurus):
    ds = dataset([buy(1, 1, 100.0, sizes=(1, 2, 4, 8)), sell(1, 2, 150.0, sizes=(1, 2, 4, 8, 16))])
    [r] = find_routes(ds, settings(centaurus))
    assert "box ?" not in r.flags


def test_unknown_container_sizes_are_flagged(centaurus):
    ds = dataset([buy(1, 1, 100.0, sizes=None), sell(1, 2, 150.0)])
    [r] = find_routes(ds, settings(centaurus))
    assert "box ?" in r.flags


def test_pad_too_small_is_excluded_unknown_is_flagged(centaurus):
    prices = [buy(1, 1, 100.0), sell(1, 2, 150.0)]
    small = dataset(prices, terminals=[terminal(1, 10, pads=frozenset({"S", "M"})), terminal(2, 20)])
    assert find_routes(small, settings(centaurus)) == []
    bigger = dataset(prices, terminals=[terminal(1, 10, pads=frozenset({"XL"})), terminal(2, 20)])
    assert len(find_routes(bigger, settings(centaurus))) == 1
    unknown = dataset(prices, terminals=[terminal(1, 10, pads=None), terminal(2, 20)])
    [r] = find_routes(unknown, settings(centaurus))
    assert "pad ?" in r.flags
    assert find_routes(unknown, settings(centaurus, show_unknown_pad=False)) == []
    no_landing = dataset(prices, terminals=[terminal(1, 10, pads=frozenset()), terminal(2, 20)])
    assert find_routes(no_landing, settings(centaurus)) == []


# --- filters -------------------------------------------------------------------

def test_illegal_commodities_are_excluded_by_default(centaurus):
    ds = dataset([buy(3, 1, 100.0), sell(3, 2, 900.0)])
    assert find_routes(ds, settings(centaurus)) == []
    [r] = find_routes(ds, settings(centaurus, include_illegal=True))
    assert r.commodity.is_illegal


def test_offline_terminals_are_excluded(centaurus):
    ds = dataset([buy(1, 1, 100.0), sell(1, 2, 150.0)], terminals=[terminal(1, 10, live=False), terminal(2, 20)])
    assert find_routes(ds, settings(centaurus)) == []


def test_routes_never_cross_systems(centaurus):
    t = [terminal(1, 10), terminal(2, 20, system=64)]
    ds = dataset([buy(1, 1, 100.0), sell(1, 2, 150.0)], terminals=t)
    assert find_routes(ds, settings(centaurus, system_ids=frozenset({68, 64}))) == []


# --- mixed cargo ---------------------------------------------------------------

def test_mixed_cargo_fills_the_hold_with_the_next_best_commodity(centaurus):
    ds = dataset([buy(1, 1, 100.0, stock=100), sell(1, 2, 200.0, demand=1000),   # +100/SCU, only 100 in stock
                  buy(2, 1, 100.0, stock=500), sell(2, 2, 140.0, demand=1000)])  # +40/SCU
    single = find_routes(ds, settings(centaurus))
    assert {r.qty for r in single} == {100, 240}
    [mixed] = find_routes(ds, settings(centaurus, mixed_cargo=True))
    assert [(c.commodity.name, c.qty) for c in mixed.cargo] == [("Gold", 100), ("Tin", 140)]
    assert mixed.qty == 240
    assert mixed.profit == pytest.approx(100 * 100 + 140 * 40 - 200)


def test_mixed_cargo_respects_budget(centaurus):
    ds = dataset([buy(1, 1, 1000.0, stock=100), sell(1, 2, 2000.0, demand=1000),
                  buy(2, 1, 1000.0, stock=500), sell(2, 2, 1400.0, demand=1000)])
    [mixed] = find_routes(ds, settings(centaurus, mixed_cargo=True, wallet=150_000 + 150_000))
    assert mixed.investment <= 150_000
    assert [(c.commodity.name, c.qty) for c in mixed.cargo] == [("Gold", 100), ("Tin", 50)]


# --- the real snapshot ---------------------------------------------------------

@pytest.mark.parametrize("wallet", [160_000, 600_000, 2_257_549, 25_000_000])
def test_real_data_routes_respect_budget_hold_and_rules(snapshot_ds, snapshot_now, centaurus, wallet):
    s = Settings(wallet=wallet, reserve=150_000, ship=centaurus, now=snapshot_now,
                 system_ids=frozenset({68}), top_n=500)
    routes = find_routes(snapshot_ds, s)
    assert routes, "the Stanton snapshot should yield routes"
    for r in routes:
        assert r.investment <= wallet - 150_000
        assert 0 < r.qty <= 240
        assert r.profit > 0
        assert r.buy_terminal.system_id == r.sell_terminal.system_id == 68
        assert not r.commodity.is_illegal
        assert r.buy_terminal.id != r.sell_terminal.id
    scores = [r.score for r in routes]
    assert scores == sorted(scores, reverse=True)


def test_real_data_system_scope(snapshot_ds, snapshot_now, centaurus):
    pyro = snapshot_ds.system_id("Pyro")
    s = Settings(wallet=5_000_000, reserve=150_000, ship=centaurus, now=snapshot_now,
                 system_ids=frozenset({pyro}), top_n=100)
    routes = find_routes(snapshot_ds, s)
    assert routes
    assert all(r.buy_terminal.system == r.sell_terminal.system == "Pyro" for r in routes)


def test_real_data_top_n_and_sort_modes(snapshot_ds, snapshot_now, centaurus):
    for mode, key in [("profit", "profit"), ("profit_per_hour", "profit_per_hour"), ("score", "score")]:
        s = Settings(wallet=2_257_549, reserve=150_000, ship=centaurus, now=snapshot_now,
                     system_ids=frozenset({68}), sort=mode)
        routes = find_routes(snapshot_ds, s)
        assert len(routes) == 15
        values = [getattr(r, key) for r in routes]
        assert values == sorted(values, reverse=True)
