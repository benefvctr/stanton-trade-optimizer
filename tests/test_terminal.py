"""Reading in-game commodity terminals: number parsing, full reads of two real
captures (Port Tressler buy list, Baijini Point with cargo to sell), terminal
identification, the price overlay and the background watcher."""
from dataclasses import replace

import pytest
from PIL import Image

from sto.captures import capture_rows, commodity_references, identify_terminal
from sto.models import apply_captures
from sto.ocr.terminal import (BuyRow, SellRow, TerminalScreen, choose, detect, number_candidates, price_from,
                              read_screen, scu_from, status_from)
from sto.ocr.terminal_watch import TerminalWatcher

from conftest import FIXTURES

PORT_TRESSLER = FIXTURES / "terminal" / "port-tressler-buy.webp"
BAIJINI = FIXTURES / "terminal" / "baijini-point-sell.webp"


@pytest.fixture(scope="module")
def engine():
    from sto.ocr.engine import OcrUnavailable, WindowsOcr
    try:
        return WindowsOcr()
    except OcrUnavailable as e:
        pytest.skip(f"Windows OCR unavailable: {e}")


@pytest.fixture(scope="module")
def vocab(snapshot_ds):
    return [c.name for c in snapshot_ds.commodities.values()], commodity_references(snapshot_ds)


# --- parsing ----------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Ä200/SCU", "200"),            # Windows OCR reads the ¤ sign as Ä
    ("A3,198/SCU", "3198"),
    ("a 3, 109/SCU", "3109"),
    ("n 1, 716/scU", "1716"),
    ("Ä3,563/SClJ", "3563"),
    (", 536/SCU", None),                 # the leading digit was lost: no reading at all
    ("90,000 SCU", None),                # a quantity, not a price
])
def test_price_lines(text, expected):
    assert price_from(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("90,000 SCU", "90000"), ("go, ooo scu", "90000"), ("2,100 scu", "2100"), ("Ä200/SCU", None),
])
def test_quantity_lines(text, expected):
    assert scu_from(text) == expected


def test_number_candidates_reject_a_lost_first_digit():
    assert number_candidates(",536") == []
    assert number_candidates("Ä4,536") == ["4536"]


def test_choose_handles_a_lost_and_an_extra_first_digit():
    assert choose(["536", "536", "4536"]) == 4536                    # tail of a longer reading = lost digit
    assert choose(["4536", "44536"], reference=5000) == 4536        # the ¤ read as a 4
    assert choose(["122", "122", "1122"], reference=1200) == 1122
    assert choose([]) is None


@pytest.mark.parametrize("text, expected", [
    ("MAX INVENTORY", 7), ("nx INVENTORY", None), ("MEDIUM INVENTORY", 4), ("NEDIUH INVENTORY", 4),
    ("VERY HIGH INVENTORY", 6), ("INVENTORY", None), ("OUT OF STOCK", 1),
])
def test_inventory_levels(text, expected):
    assert status_from(text) == expected


# --- full reads of real captures --------------------------------------------------------

def test_reads_the_port_tressler_buy_list(engine, vocab):
    screen = read_screen(Image.open(PORT_TRESSLER), engine, *vocab)
    got = {r.commodity: (r.price, r.stock) for r in screen.buy}
    assert got == {
        "Waste": (200, 90_000), "Pressurized Ice": (4536, 514), "Quartz": (3563, 287), "Scrap": (3109, 2100),
        "Copper": (3198, 326), "Agricultural Supplies": (1122, 6000), "DynaFlex": (1716, 823),
    }
    levels = {r.commodity: r.status for r in screen.buy if r.status is not None}
    assert levels.get("Pressurized Ice", 4) == 4 and levels.get("Scrap", 7) == 7    # never a wrong level
    assert screen.sell == [] and screen.location is None
    assert screen.balance in (None, 2_366_183)                     # small text: read only when two reads agree


def test_reads_baijini_point_with_cargo_to_sell(engine, vocab):
    screen = read_screen(Image.open(BAIJINI), engine, *vocab)
    assert screen.location == "Baijini Point"
    assert {r.commodity: (r.price, r.stock) for r in screen.buy} == {
        "Waste": (189, 100_000), "Silicon": (1964, 9752), "Scrap": (3109, 2100), "Hephaestanite": (4453, 1050)}
    assert [(r.commodity, r.price, r.cargo_scu) for r in screen.sell] == [("Pressurized Ice", 6560, 240)]
    assert screen.balance == 1_277_842


@pytest.mark.parametrize("name", ["mobiglas-home.webp", "no-mobiglas.webp"])
def test_no_terminal_no_reading(engine, name):
    assert detect(Image.open(FIXTURES / "ocr" / name).convert("RGB"), engine) is None


# --- which terminal ----------------------------------------------------------------------

def test_identifies_port_tressler_from_prices_alone(snapshot_ds):
    screen = TerminalScreen(buy=[BuyRow("Waste", 200, 90000), BuyRow("Pressurized Ice", 4536, 514),
                                 BuyRow("Quartz", 3563, 287), BuyRow("Scrap", 3109, 2100),
                                 BuyRow("Copper", 3198, 326), BuyRow("Agricultural Supplies", 1122, 6000),
                                 BuyRow("DynaFlex", 1716, 823)])
    found = identify_terminal(snapshot_ds, screen)
    assert snapshot_ds.terminals[found.terminal_id].label == "Port Tressler" and found.reason == "prices"


def test_identifies_baijini_from_the_location_line(snapshot_ds):
    screen = TerminalScreen(buy=[BuyRow("Waste", 189, 100000)], sell=[SellRow("Pressurized Ice", 6560, 240)],
                            location="Baijini Point")
    found = identify_terminal(snapshot_ds, screen)
    assert snapshot_ds.terminals[found.terminal_id].label == "Baijini Point"


def test_too_little_to_go_on_identifies_nothing(snapshot_ds):
    found = identify_terminal(snapshot_ds, TerminalScreen(buy=[BuyRow("Waste", 200, 100)]))
    assert found.terminal_id is None


# --- overlay -------------------------------------------------------------------------------

def test_capture_replaces_its_side_unless_uex_is_newer(snapshot_ds):
    # A terminal that both sells and buys the commodity (one UEX row, both sides).
    p = replace(next(p for p in snapshot_ds.prices if p.price_buy > 0), price_sell=9999.0, status_sell=1)
    base = [replace(p, updated=1000.0)]
    cap = {"terminal_id": p.terminal_id, "commodity_id": p.commodity_id, "side": "buy", "price": 4242.0,
           "scu": 77, "status": 4, "boxes": "1,2,4", "captured_at": 2000.0}
    out = apply_captures(base, [cap])
    buy = [x for x in out if x.price_buy > 0]
    assert len(buy) == 1 and buy[0].source == "you" and buy[0].price_buy == 4242 and buy[0].scu_buy == 77
    assert buy[0].updated == 2000 and buy[0].container_sizes == (1, 2, 4)
    sell = [x for x in out if x.price_sell > 0]
    assert len(sell) == 1 and sell[0].source == "uex" and sell[0].updated == 1000     # untouched, own age
    assert apply_captures([replace(p, updated=3000.0)], [cap]) == [replace(p, updated=3000.0)]  # UEX newer


def test_captured_sell_price_is_never_treated_as_full(snapshot_ds):
    p = next(p for p in snapshot_ds.prices if p.price_sell > 0)
    out = apply_captures([replace(p, status_sell=7, updated=1.0)], [{
        "terminal_id": p.terminal_id, "commodity_id": p.commodity_id, "side": "sell", "price": 999.0,
        "scu": None, "status": None, "boxes": None, "captured_at": 5.0}])
    mine = next(x for x in out if x.source == "you")
    assert mine.price_sell == 999 and mine.status_sell != 7


def test_capture_rows_skip_rows_without_a_price(snapshot_ds):
    screen = TerminalScreen(buy=[BuyRow("Waste", None, 500), BuyRow("Quartz", 3563, 287)])
    rows = capture_rows(snapshot_ds, screen, 66, 123.0)
    assert [(r["side"], r["price"], r["scu"]) for r in rows] == [("buy", 3563, 287)]


# --- watcher -------------------------------------------------------------------------------

def make_watcher(engine, vocab, window, image, seen):
    clock = {"t": 0.0}
    w = TerminalWatcher(context=lambda: vocab, on_screen=seen.append, engine=engine, window=window,
                        grab=lambda rect: image, repeat_s=60, clock=lambda: clock["t"])
    return w, clock


def test_watcher_only_reads_while_the_game_is_focused(engine, vocab):
    seen = []
    w, _ = make_watcher(engine, vocab, lambda: None, Image.open(PORT_TRESSLER), seen)
    assert w.poll_once() is None and seen == []


def test_watcher_reports_a_terminal_once_until_it_changes(engine, vocab):
    seen = []
    w, clock = make_watcher(engine, vocab, lambda: (0, 0, 3440, 1440), Image.open(PORT_TRESSLER).convert("RGB"),
                            seen)
    assert w.poll_once() is not None and len(seen) == 1
    clock["t"] = 10
    assert w.poll_once() is None and len(seen) == 1                   # same screen, too soon
    clock["t"] = 100
    assert w.poll_once() is not None and len(seen) == 2               # same screen, a minute later
    w.grab = lambda rect: Image.open(FIXTURES / "ocr" / "no-mobiglas.webp").convert("RGB")
    assert w.poll_once() is None                                      # terminal closed
    w.grab = lambda rect: Image.open(PORT_TRESSLER).convert("RGB")
    clock["t"] = 101
    assert w.poll_once() is not None and len(seen) == 3               # opened again: a new reading
