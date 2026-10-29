"""SC Trade Tools community reports: parsing, paging, matching reports to UEX
terminals (gateways come without their system), the outlier guard, the overlay
and the hourly pull. Report rows are written in the feed's real format."""
import json
from datetime import datetime, timezone

import pytest

from sto import sctrade
from sto.data import SCTRADE_RETRY_S, DataService
from sto.models import build_dataset
from sto.store import Store
from sto.uex import RateLimiter, UexClient

from test_data_layer import FakeClock, snapshot_transport


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat(timespec="milliseconds")


def report(location, commodity, transaction, price, at, qty=100, sat=0.5, boxes=None):
    return {"location": location, "transaction": transaction, "commodity": commodity, "price": price,
            "quantity": qty, "saturation": sat, "boxSizesInScu": boxes, "batchId": "b", "timestamp": iso(at)}


def rows(*reports):
    return [sctrade.parse_listing(r) for r in reports]


def terminal(ds, label, system="Stanton"):
    return next(t for t in ds.terminals.values() if t.label == label and t.system == system)


def uex_price(ds, terminal_id, name, side):
    cid = next(c.id for c in ds.commodities.values() if c.name == name)
    p = next(p for p in ds.prices if p.terminal_id == terminal_id and p.commodity_id == cid)
    return cid, p, (p.price_buy if side == "buy" else p.price_sell)


def merged(ds, raw, listings, captures=None):
    return build_dataset(raw, listings=listings, captures=captures)


# --- parsing and paging -------------------------------------------------------------

def test_parses_a_report_into_a_store_row():
    r = sctrade.parse_listing(report("stanton > crusader > seraphim station", "Copper", "SELLS", 3198, 1_800_000_000,
                                     qty=1704, sat=0.5, boxes=[1, 2, 4]))
    assert r == {"location": "stanton > crusader > seraphim station", "commodity": "copper", "side": "buy",
                 "price": 3198.0, "scu": 1704, "saturation": 0.5, "boxes": "1,2,4", "observed_at": 1_800_000_000.0}
    assert sctrade.parse_listing(report("x", "copper", "BUYS", 10, 1.0))["side"] == "sell"    # the shop buys


@pytest.mark.parametrize("bad", [
    {"transaction": "SELLS"}, report("x", "copper", "TRADES", 10, 1.0), report("x", "copper", "SELLS", 0, 1.0),
    {**report("x", "copper", "SELLS", 10, 1.0), "timestamp": "yesterday"},
])
def test_malformed_reports_are_skipped(bad):
    assert sctrade.parse_listing(bad) is None


def pager(pages):
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        page = int(url.rsplit("=", 1)[1])
        content = pages[page] if page < len(pages) else []
        return 200, json.dumps({"content": content, "page": {"number": page}})

    return transport, calls


def test_fetch_stops_at_the_newest_report_already_stored():
    pages = [[report("x", "copper", "SELLS", 1, t) for t in (1000, 990)], [report("x", "copper", "SELLS", 1, 900)]]
    transport, calls = pager(pages)
    got = sctrade.fetch_listings(since=995, oldest=0, transport=transport, sleep=lambda s: None)
    assert [r["observed_at"] for r in got] == [1000] and len(calls) == 1


def test_fetch_pages_back_until_reports_get_too_old():
    pages = [[report("x", "copper", "SELLS", 1, t)] for t in (1000, 900, 800, 700)]
    transport, calls = pager(pages)
    got = sctrade.fetch_listings(since=0, oldest=850, transport=transport, sleep=lambda s: None)
    assert [r["observed_at"] for r in got] == [1000, 900] and len(calls) == 3


@pytest.mark.parametrize("answer", [(503, "{}"), (200, "<html>"), OSError("offline")])
def test_fetch_failures_raise_one_error_type(answer):
    def transport(url, headers, timeout):
        if isinstance(answer, Exception):
            raise answer
        return answer

    with pytest.raises(sctrade.SctradeError):
        sctrade.fetch_listings(0, 0, transport=transport, sleep=lambda s: None)


# --- matching and the outlier guard ---------------------------------------------------------

def test_a_newer_report_replaces_that_side_of_the_uex_price(snapshot_ds, snapshot_raw, snapshot_now):
    pt = terminal(snapshot_ds, "Port Tressler")
    cid, p, price = uex_price(snapshot_ds, pt.id, "Scrap", "buy")
    at = max(p.updated, snapshot_now - 3600) + 60
    ds = merged(snapshot_ds, snapshot_raw, rows(report("stanton > microtech > port tressler", "scrap", "SELLS",
                                                       price + 50, at, qty=2100, sat=1.0, boxes=[1, 2])))
    mine = [x for x in ds.prices if x.terminal_id == pt.id and x.commodity_id == cid and x.price_buy > 0]
    assert len(mine) == 1 and mine[0].source == "stt" and mine[0].price_buy == price + 50
    assert mine[0].scu_buy == 2100 and mine[0].status_buy == 7 and mine[0].container_sizes == (1, 2)
    assert mine[0].updated == at and ds.sctrade_used == 1


def test_an_older_report_than_uex_changes_nothing(snapshot_ds, snapshot_raw):
    pt = terminal(snapshot_ds, "Port Tressler")
    cid, p, price = uex_price(snapshot_ds, pt.id, "Scrap", "buy")
    ds = merged(snapshot_ds, snapshot_raw, rows(report("stanton > microtech > port tressler", "scrap", "SELLS",
                                                       price + 50, p.updated - 60)))
    assert ds.sctrade_used == 0


def test_a_misread_digit_is_rejected_and_the_older_good_report_used(snapshot_ds, snapshot_raw, snapshot_now):
    pt = terminal(snapshot_ds, "Port Tressler")
    cid, p, price = uex_price(snapshot_ds, pt.id, "Scrap", "buy")
    at = max(p.updated, snapshot_now - 3600) + 60
    loc = "stanton > microtech > port tressler"
    ds = merged(snapshot_ds, snapshot_raw, rows(report(loc, "scrap", "SELLS", 40000 + price, at + 600),   # ¤ read as 4
                                                report(loc, "scrap", "SELLS", price + 10, at)))
    mine = next(x for x in ds.prices if x.terminal_id == pt.id and x.commodity_id == cid and x.price_buy > 0)
    assert mine.source == "stt" and mine.price_buy == price + 10


def gateway_commodity(ds, a, b, side):
    """A commodity both gateway terminals trade on `side`, at different prices."""
    at = lambda t: {x.commodity_id: (x.price_buy if side == "buy" else x.price_sell)  # noqa: E731
                    for x in ds.prices if x.terminal_id == t.id}
    pa, pb = at(a), at(b)
    cid = next(c for c in pa if pa[c] > 0 and pb.get(c, 0) > 0 and abs(pa[c] - pb[c]) / pa[c] > 0.05)
    return ds.commodities[cid].name, pa[cid], pb[cid]


def test_a_gateway_named_without_its_system_goes_to_the_closer_uex_price(snapshot_ds, snapshot_raw, snapshot_now):
    stanton, nyx = terminal(snapshot_ds, "Pyro Gateway"), terminal(snapshot_ds, "Pyro Gateway", "Nyx")
    for side in ("sell", "buy"):
        try:
            name, at_stanton, at_nyx = gateway_commodity(snapshot_ds, stanton, nyx, side)
            break
        except StopIteration:
            continue
    transaction = "BUYS" if side == "sell" else "SELLS"
    ds = merged(snapshot_ds, snapshot_raw, rows(report("pyro gateway", name.lower(), transaction, at_nyx,
                                                       snapshot_now)))
    won = [x for x in ds.prices if x.source == "stt"]
    assert len(won) == 1 and won[0].terminal_id == nyx.id


def test_never_guesses_between_two_systems(snapshot_ds):
    index = sctrade.place_index(snapshot_ds.terminals)
    uex = {(p.terminal_id, p.commodity_id): p for p in snapshot_ds.prices}
    traded = __import__("collections").Counter(p.terminal_id for p in snapshot_ds.prices)
    systems = {"stanton", "pyro", "nyx"}
    priced = {cid for (tid, cid) in uex if tid in {t.id for t in index["nyx gateway"]}}
    unpriced = next(c.id for c in snapshot_ds.commodities.values() if c.id not in priced)
    assert sctrade.match_terminal("nyx gateway", unpriced, "sell", 100, index, uex, systems, traded) is None
    # Within one system the station's main terminal takes it, not its single-item side shop.
    t = sctrade.match_terminal("stanton > nyx gateway", unpriced, "sell", 100, index, uex, systems, traded)
    assert t is not None and t.label == "Nyx Gateway"


def test_unknown_places_and_commodities_are_skipped(snapshot_ds, snapshot_raw, snapshot_now):
    ds = merged(snapshot_ds, snapshot_raw, rows(report("stanton > nowhere", "scrap", "SELLS", 3000, snapshot_now),
                                                report("stanton > microtech > port tressler", "unobtainium",
                                                       "SELLS", 3000, snapshot_now)))
    assert ds.sctrade_used == 0


def test_a_full_shop_report_marks_the_sell_side_full(snapshot_ds, snapshot_raw, snapshot_now):
    p = next(x for x in snapshot_ds.prices if x.price_sell > 0 and snapshot_ds.terminals[x.terminal_id].system
             == "Stanton" and snapshot_ds.terminals[x.terminal_id].label == "Port Tressler")
    name = snapshot_ds.commodities[p.commodity_id].name
    ds = merged(snapshot_ds, snapshot_raw, rows(report("stanton > microtech > port tressler", name.lower(), "BUYS",
                                                       p.price_sell, snapshot_now, sat=1.0)))
    mine = next(x for x in ds.prices if x.source == "stt")
    assert mine.price_sell == p.price_sell and mine.status_sell == 7


def test_your_own_terminal_read_beats_an_older_report(snapshot_ds, snapshot_raw, snapshot_now):
    pt = terminal(snapshot_ds, "Port Tressler")
    cid, p, price = uex_price(snapshot_ds, pt.id, "Scrap", "buy")
    capture = {"terminal_id": pt.id, "commodity_id": cid, "side": "buy", "price": price + 1, "scu": 5,
               "status": 4, "boxes": None, "captured_at": snapshot_now}
    ds = merged(snapshot_ds, snapshot_raw, rows(report("stanton > microtech > port tressler", "scrap", "SELLS",
                                                       price + 50, snapshot_now - 60)), [capture])
    mine = [x for x in ds.prices if x.terminal_id == pt.id and x.commodity_id == cid and x.price_buy > 0]
    assert [(x.source, x.price_buy) for x in mine] == [("you", price + 1)]


# --- the hourly pull ----------------------------------------------------------------------------

def sct_service(snapshot_raw, tmp_path, transport):
    uex, _ = snapshot_transport(snapshot_raw)
    clock = FakeClock(snapshot_raw["fetched_at"])
    client = UexClient(transport=uex, sleep=clock.sleep, limiter=RateLimiter(1000, clock=clock, sleep=clock.sleep))
    svc = DataService(client, Store(tmp_path / "c.sqlite"), clock=clock, sleep=clock.sleep,
                      sctrade_transport=transport)
    return svc, clock


def test_refresh_pulls_reports_hourly_and_uses_them(snapshot_raw, snapshot_ds, tmp_path):
    now = float(snapshot_raw["fetched_at"])
    pt = terminal(snapshot_ds, "Port Tressler")
    _, p, price = uex_price(snapshot_ds, pt.id, "Scrap", "buy")
    transport, calls = pager([[report("stanton > microtech > port tressler", "scrap", "SELLS", price + 50, now - 60)]])
    svc, clock = sct_service(snapshot_raw, tmp_path, transport)
    assert svc.refresh() == [] and "sctrade" in svc.fetched
    assert len(calls) == 2                                # the first pull reads back until the feed runs out
    assert svc.dataset().sctrade_used == 1
    clock.t += 600
    svc.refresh()
    assert len(calls) == 2                                # not due for an hour
    clock.t += 3600
    svc.refresh()
    assert len(calls) == 3                                # one page: nothing newer than what's stored
    assert len(svc.store.listings()) == 1


def test_a_failing_feed_never_fails_the_uex_refresh(snapshot_raw, tmp_path):
    calls = []

    def down(url, headers, timeout):
        calls.append(url)
        return 502, "Bad Gateway"

    svc, clock = sct_service(snapshot_raw, tmp_path, down)
    assert svc.refresh() == []                            # UEX fine, so no errors to show
    assert "SC Trade Tools: HTTP 502" in svc.sctrade_status()["last_error"]
    clock.t += SCTRADE_RETRY_S - 60
    svc.refresh()
    assert len(calls) == 1                                # backs off before trying again
    clock.t += 3600
    svc.refresh()
    assert len(calls) == 2


def test_switched_off_means_no_requests_and_no_reports(snapshot_raw, tmp_path):
    svc, _ = sct_service(snapshot_raw, tmp_path, None)
    svc.refresh()
    assert svc.sctrade_status() is None and svc.dataset().sctrade_used == 0
