"""Drive the Textual app headlessly: every keybind, OCR updates and the stale
banner, against the real snapshot served by a fake UEX transport."""
import shutil

import pytest
from textual.widgets import DataTable, Static
from textual.worker import WorkerCancelled

from sto.config import ROOT, Config
from sto.data import DataService
from sto.ocr.sync import Read
from sto.store import Store
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


def make_app(app_parts, **kw):
    cfg, svc, store, clock, _ = app_parts
    kw.setdefault("start_ocr", False)
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
        assert app.wallet == 2_366_183 and app.wallet_source == "ocr"
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
