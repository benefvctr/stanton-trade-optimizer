"""Drive the Textual app headlessly: every keybind, OCR updates and the stale
banner, against the real snapshot served by a fake UEX transport."""
import shutil

import pytest
from rich.text import Text
from textual.widgets import DataTable, Static
from textual.worker import WorkerCancelled

from sto.config import ROOT, Config
from sto.data import DataService
from sto.ocr.sync import Read
from sto.store import Store
from sto.tui import fmt
from sto.tui.app import TradeApp
from sto.uex import RateLimiter, UexClient

from test_data_layer import FakeClock, snapshot_transport

SIZE = (140, 46)


class FakeSync:
    instances = []

    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        FakeSync.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        pass

    def next_read_at(self):
        return None

    def grab(self, monitor, box):
        from PIL import Image
        return Image.new("RGB", (box[2], box[3]))

    def read_image(self, image, apply=True):
        return Read(0, "2,257,549", 2_257_549, True, "test", "fake")

    def engine(self):
        from sto.ocr.engine import OcrUnavailable, WindowsOcr
        try:
            return WindowsOcr()
        except OcrUnavailable as e:
            pytest.skip(f"Windows OCR unavailable: {e}")


@pytest.fixture
def app_parts(snapshot_raw, tmp_path):
    cfg_path = tmp_path / "config.toml"
    shutil.copy(ROOT / "config.default.toml", cfg_path)
    cfg = Config(cfg_path)
    cfg.set("wallet", "balance", 2_257_549)
    cfg.save()
    transport, state = snapshot_transport(snapshot_raw)
    clock = FakeClock(snapshot_raw["fetched_at"])
    client = UexClient(transport=transport, sleep=clock.sleep, limiter=RateLimiter(1000, clock=clock, sleep=clock.sleep))
    store = Store(tmp_path / "c.sqlite")
    svc = DataService(client, store, clock=clock, sleep=clock.sleep)
    return cfg, svc, store, clock, state


class FakeWatcher:
    """Stands in for the terminal watcher, which would capture the real screen."""

    def __init__(self, **kw):
        self.kw = kw

    def start(self):
        pass

    def stop(self):
        pass


def make_app(app_parts, **kw):
    cfg, svc, store, clock, _ = app_parts
    kw.setdefault("start_ocr", False)
    kw.setdefault("terminal_factory", FakeWatcher)
    return TradeApp(cfg, svc, store, clock=clock, **kw)


async def settle(app, pilot):
    for _ in range(4):
        await pilot.pause()
        try:
            await app.workers.wait_for_complete()
        except WorkerCancelled:
            pass
    await pilot.pause()


def rows(app):
    return app.query_one("#routes", DataTable).row_count


def status(app):
    return str(app.query_one("#status", Static).render())


async def test_loads_routes_and_shows_header(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert rows(app) == 15
        text = status(app)
        assert "2,257,549 aUEC" in text and "CONFIG" in text
        assert "Constellation Mk V Centaurus" in text
        assert "STANTON" in text
        assert all(r.buy_terminal.system == "Stanton" for r in app.routes)
        assert app.query_one("#routes").border_title.startswith("▌ROUTES")


async def test_manual_wallet_entry_recomputes(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("w")
        await pilot.press(*"400,000", "enter")
        await settle(app, pilot)
        assert app.wallet == 400_000 and app.wallet_source == "manual"
        assert all(r.investment <= 250_000 for r in app.routes)
        assert app.store.get_state("wallet")["value"] == 400_000


async def test_toggles_sort_illegal_systems_mixed(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("o")
        assert app.sort == "profit"
        profits = [r.profit for r in app.routes]
        assert profits == sorted(profits, reverse=True)
        await pilot.press("o")
        assert app.sort == "profit_per_hour"
        await pilot.press("o")
        assert app.sort == "score"
        await pilot.press("l")
        assert app.include_illegal
        await pilot.press("y")
        await settle(app, pilot)
        assert app.routes and all(r.buy_terminal.system == "Pyro" for r in app.routes)
        await pilot.press("m")
        assert app.mixed


def place_cells(app):
    table = app.query_one("#routes", DataTable)
    return [tuple(str(c) for c in table.get_row_at(i)[2:4]) for i in range(table.row_count)]


async def test_places_carry_a_system_tag_only_when_systems_mix(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert all(buy == r.buy_terminal.label for (buy, _), r in zip(place_cells(app), app.routes))
        await pilot.press("y", "y", "y")                                # Pyro, Nyx, then all three
        await settle(app, pilot)
        assert app.scopes[app.scope_idx] == ["Stanton", "Pyro", "Nyx"]
        tags = {"Stanton": "STAN ", "Pyro": "PYRO ", "Nyx": "NYX  "}
        for (buy, sell), r in zip(place_cells(app), app.routes):
            assert buy == tags[r.buy_terminal.system] + r.buy_terminal.label
            assert sell == tags[r.sell_terminal.system] + r.sell_terminal.label
        widths = {c.label.plain: c.width for c in app.query_one("#routes", DataTable).columns.values()}
        assert widths["BUY AT"] == widths["SELL AT"] == 19
        table = app.query_one("#routes", DataTable)
        assert table.virtual_size.width <= table.scrollable_content_region.width     # no sideways scrolling
        assert buy_title(app) == f"▌BUY ▸ {buy_cell(app)}"
        await pilot.press("y")                                          # back to Stanton alone
        await settle(app, pilot)
        assert all(not buy.startswith("STAN ") for buy, _ in place_cells(app))
        assert buy_title(app) == f"▌BUY ▸ {app.routes[0].buy_terminal.label}"


def buy_title(app):
    return Text.from_markup(str(app.query_one("#buy").border_title)).plain


def buy_cell(app):
    return place_cells(app)[app.query_one("#routes", DataTable).cursor_row][0]


def test_every_live_system_has_its_own_tag():
    assert [str(fmt.place("Checkmate", s, True)) for s in ("Stanton", "Pyro", "Nyx", "Castra")] == [
        "STAN Checkmate", "PYRO Checkmate", "NYX  Checkmate", "CAST Checkmate"]
    assert fmt.place("Checkmate", "Pyro", False) == "Checkmate"


async def test_ship_switch_saves_config(app_parts):
    cfg = app_parts[0]
    from sto.config import ShipProfile
    cfg.add_ship(ShipProfile("MISC Hull C", 4608, 32, "XL", uex_id=1))
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("s")
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert app.ship.name == "MISC Hull C"
        assert Config(cfg.path).get("ship", "active") == "MISC Hull C"
        assert max(r.qty for r in app.routes) > 240


async def test_help_opens_and_closes(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("question_mark")
        assert app.screen.__class__.__name__ == "HelpScreen"
        await pilot.press("escape")
        assert app.screen.__class__.__name__ != "HelpScreen"


async def test_calibrate_by_coordinates_then_test_read(app_parts):
    app = make_app(app_parts, start_ocr=True, sync_factory=FakeSync)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert FakeSync.instances[-1].started
        await pilot.press("c")
        await pilot.press("e")
        await pilot.press(*"560 752 90 22 1", "enter")
        await settle(app, pilot)
        assert app.cfg.ocr_region() == (1, (560, 752, 90, 22))
        assert Config(app.cfg.path).ocr_region() == (1, (560, 752, 90, 22))


async def test_calibrate_from_a_screenshot_without_dragging(app_parts, monkeypatch):
    from sto.ocr import capture
    from test_ocr_calibrate import ULTRAWIDE
    from conftest import FIXTURES

    monkeypatch.setattr(capture, "monitors", lambda: ULTRAWIDE)
    app = make_app(app_parts, start_ocr=True, sync_factory=FakeSync)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("c")
        await pilot.press("f")
        box = app.screen.query_one("#file")
        box.value = str(FIXTURES / "ocr" / "mobiglas-home-2.webp")
        await pilot.press("enter")
        await settle(app, pilot)
        where = app.cfg.ocr_region()
        assert where is not None and where[0] == 1
        x, y, w, h = where[1]
        assert x <= 600 * 1.72 <= x + w and y <= 765 * 1.72 <= y + h
        assert app.wallet == 2_257_549            # an old screenshot never changes the wallet


async def test_ocr_read_updates_wallet_and_routes(app_parts):
    app = make_app(app_parts, start_ocr=True, sync_factory=FakeSync)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        before = [r.qty for r in app.routes]
        app.on_ocr_read(Read(app.clock(), "¤ 300,000", 300_000, True, "accepted", "fake"))
        await settle(app, pilot)
        assert app.wallet == 300_000 and app.wallet_source == "ocr"
        assert "OCR" in status(app)
        assert [r.qty for r in app.routes] != before
        app.on_ocr_read(Read(app.clock(), "9,000,000", 9_000_000, False, "waiting for a second matching read", "fake"))
        assert app.wallet == 300_000
        assert "waiting" in app.ocr_status


async def test_a_crash_inside_a_refresh_never_closes_the_app(app_parts):
    cfg, svc, store, clock, state = app_parts
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        def boom(*a, **k):
            raise RuntimeError("something unexpected")
        svc.refresh = boom
        await pilot.press("r")
        await settle(app, pilot)
        assert app.is_running
        assert rows(app) == 15
        assert "something unexpected" in app.last_errors[0]


async def test_refresh_key_and_stale_banner(app_parts):
    _, _, _, clock, state = app_parts
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        state["calls"].clear()
        await pilot.press("r")
        await settle(app, pilot)
        assert "commodities_prices_all" in state["calls"]
        state["fail_prices"] = True
        clock.t += 120
        await pilot.press("r")
        await settle(app, pilot)
        banner = app.query_one("#banner", Static)
        assert "show" in banner.classes
        assert "STALE SINCE" in str(banner.render())
        assert rows(app) == 15                       # still ranking from the cache


# --- ships ---------------------------------------------------------------------

async def open_add_screen(app, pilot, query):
    await pilot.press("s")
    await pilot.press("a")
    await settle(app, pilot)
    assert app.screen.__class__.__name__ == "AddShipScreen"
    await pilot.press(*query)
    await pilot.pause()


async def test_add_a_ship_by_searching_the_uex_list(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await open_add_screen(app, pilot, "hull c")
        assert app.screen.shown[0]["name_full"] == "MISC Hull C"      # whole-phrase match ranks first
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.ship.name == "MISC Hull C"
        assert (app.ship.cargo_scu, app.ship.max_container_scu, app.ship.pad) == (4608, 32, "L")
        saved = Config(app.cfg.path)
        assert saved.get("ship", "active") == "MISC Hull C" and "MISC Hull C" in saved.ships()
        assert max(r.qty for r in app.routes) > 240


async def test_the_list_hides_concept_ships_and_filters_as_you_type(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await open_add_screen(app, pilot, "")
        names = [v["name_full"] for v in app.screen.shown]
        assert "MISC Hull E" not in names and "Anvil Crucible" not in names     # concepts
        assert names[0] == "MISC Hull C"                                        # biggest flyable hold first
        await pilot.press(*"drake")
        assert app.screen.shown and all("drake" in v["name_full"].lower() for v in app.screen.shown)
        await pilot.press("down", "down", "enter")
        await settle(app, pilot)
        assert app.ship.name == [v["name_full"] for v in app.svc.cargo_ships() if "Drake" in v["name_full"]][2]


async def test_missing_box_and_pad_are_picked_from_a_list(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await open_add_screen(app, pilot, "golem ox")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ChoiceScreen"          # biggest box, defaults to 32
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ChoiceScreen"          # pad, defaults to "don't know"
        await pilot.press("up", "up", "up", "enter")                    # ... XL, L, M -> M
        await settle(app, pilot)
        assert app.ship.name == "Drake Golem Ox"
        assert (app.ship.cargo_scu, app.ship.max_container_scu, app.ship.pad) == (64, 32, "M")


async def test_remove_a_ship_but_never_the_active_one(app_parts):
    from sto.config import ShipProfile
    cfg = app_parts[0]
    cfg.add_ship(ShipProfile("MISC Hull C", 4608, 32, "L", uex_id=1))
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("s", "delete")
        assert "Switch first" in str(app.screen.query_one("#err").render())
        await pilot.press("down", "delete")
        await settle(app, pilot)
        assert "MISC Hull C" not in Config(cfg.path).ships()
        assert app.screen.__class__.__name__ == "ShipScreen"            # reopened to show what's left
        await pilot.press("escape")
        assert app.ship.name == "Constellation Mk V Centaurus"


async def test_ctrl_r_refreshes_the_ship_list_from_uex(app_parts):
    state = app_parts[4]
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await open_add_screen(app, pilot, "")
        state["calls"].clear()
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert state["calls"] == ["vehicles"]
        assert app.screen.__class__.__name__ == "AddShipScreen" and app.screen.shown


async def test_a_default_size_console_window_still_shows_the_routes(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(app, pilot)
        assert not app.query_one("#wordmark").display      # too narrow for the logo
        assert not app.query_one("#detail").display        # too short for the detail pane
        assert rows(app) == 15
        assert app.query_one("#routes").size.height >= 10
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.query_one("#wordmark").display and app.query_one("#detail").display


async def test_a_terminal_capture_updates_routes_status_and_wallet(app_parts):
    from sto.ocr.terminal import BuyRow, TerminalScreen

    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        screen = TerminalScreen(
            buy=[BuyRow("Waste", 200, 90000), BuyRow("Pressurized Ice", 4536, 514, 4), BuyRow("Quartz", 3563, 287),
                 BuyRow("Scrap", 3109, 2100), BuyRow("Copper", 3198, 326), BuyRow("Agricultural Supplies", 1122, 6000),
                 BuyRow("DynaFlex", 1716, 823)],
            balance=2_366_183)
        app.terminal_seen(screen)
        await settle(app, pilot)
        saved = app.store.captures()
        assert len(saved) == 7 and all(c["side"] == "buy" for c in saved)
        assert "Port Tressler" in app.terminal_status
        assert app.wallet == 2_366_183 and app.wallet_source == "terminal"
        assert app.store.wallet_log()[-1].source == "terminal"
        ice = next(p for p in app.ds.prices if p.source == "you" and p.price_buy == 4536)
        assert ice.scu_buy == 514 and ice.updated == pytest.approx(app.clock())
        tressler = [r for r in app.routes if r.buy_terminal.label == "Port Tressler"]
        assert all("you" in r.flags for r in tressler)


async def test_an_unidentified_terminal_saves_nothing(app_parts):
    from sto.ocr.terminal import BuyRow, TerminalScreen

    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.terminal_seen(TerminalScreen(buy=[BuyRow("Waste", 200, 100)]))
        await settle(app, pilot)
        assert app.store.captures() == []
        assert "couldn't tell" in app.terminal_status


async def test_all_systems_show_crossing_runs_until_jumps_are_switched_off(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.cross and not any(r.via for r in app.routes)       # Stanton alone: nothing to cross
        await pilot.press("y", "y", "y")
        await settle(app, pilot)
        crossing = [i for i, r in enumerate(app.routes) if r.via]
        assert crossing and "JUMPS" in app.query_one("#routes", DataTable).border_title
        i = crossing[0]
        r = app.routes[i]
        buy, sell = place_cells(app)[i]
        assert buy.split()[0] != sell.split()[0]                      # two different system tags
        app.show_detail(i)
        run = str(app.query_one("#run", Static).render())
        assert f"jump     {r.buy_terminal.system} → {r.sell_terminal.system} via {r.via}" in run
        await pilot.press("x")
        await settle(app, pilot)
        assert not app.cross and not any(r.via for r in app.routes)
        assert "JUMPS" not in app.query_one("#routes", DataTable).border_title
        assert "JUMPS OFF" in status(app)


async def test_sc_trade_tools_reports_show_in_the_header_and_the_detail(app_parts):
    from sto.optimizer import find_routes
    from sto.sctrade import _plain
    from test_sctrade import pager, report

    cfg, svc, store, clock, _ = app_parts
    svc.refresh()
    ds = svc.dataset()
    probe = make_app(app_parts)
    probe.ds = ds
    top = find_routes(ds, probe.settings())[0]                       # aim a report at the best run's buy price
    t, c = top.buy_terminal, top.cargo[0]
    where = f"{t.system.lower()} > {_plain(t.orbit, t.system)} > {_plain(t.place, t.system)}"
    transport, _ = pager([[report(where, c.commodity.name.lower(), "SELLS", c.buy.price_buy - 1, clock() - 60)]])
    svc.sctrade_transport = transport
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert "SC TRADE" in status(app) and "1 newer than UEX" in status(app)
        i = next(i for i, r in enumerate(app.routes) if "stt" in r.flags)
        assert "◆" in str(app.query_one("#routes", DataTable).get_row_at(i)[-1])
        app.show_detail(i)
        assert "◆ SC Trade Tools" in str(app.query_one("#buy", Static).render())


async def test_the_header_shows_what_the_last_run_and_the_session_earned(app_parts):
    app = make_app(app_parts)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert "needs a balance reading" in status(app)
        t0 = app.clock()
        app.set_wallet(2_000_000, "ocr", t0)                       # before buying
        app.set_wallet(1_500_000, "terminal", t0 + 60)             # cargo bought
        await pilot.pause()
        assert "▼ -500,000" in status(app) and "in cargo" in status(app)
        app.set_wallet(2_400_000, "terminal", t0 + 1500)           # sold
        await pilot.pause()
        assert "▲ +400,000 last run" in status(app) and "SESSION +400,000" in status(app)
        await pilot.press("h")
        await pilot.pause()
        history = str(app.screen.query_one("#history", Static).render())
        assert "2,400,000" in history and "+900,000" in history and "-500,000" in history
        assert history.index("2,400,000") < history.index("1,500,000")     # newest first
        await pilot.press("escape")


async def test_the_ocr_line_says_when_f1_reads_again(app_parts):
    app = make_app(app_parts, start_ocr=True, sync_factory=FakeSync)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        FakeSync.instances[-1].next_read_at = lambda: app.clock() + 120
        app.update_status()
        assert "F1 reads again at" in status(app)
