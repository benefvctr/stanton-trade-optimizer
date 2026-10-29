"""The trade terminal: header, route table, detail pane, keybinds."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Static

from ..config import LOG_DIR, SORT_MODES, Config, ShipProfile, vehicle_max_box
from ..data import DataService
from ..distance import missing_orbit_pairs
from ..models import Dataset
from ..ocr.sanity import WalletSanity
from ..optimizer import PAD_ORDER, Route, Settings, find_routes
from ..store import Store
from . import fmt
from .banner import wordmark_markup
from .screens import AddShipScreen, CalibrateScreen, ChoiceScreen, HelpScreen, ShipScreen, WalletScreen

def app_log() -> logging.Logger:
    log = logging.getLogger("sto.app")
    if not log.handlers:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.propagate = False
    return log


PHOSPHOR = Theme(
    name="sto-phosphor",
    primary="#ff8a1f",
    secondary="#ffb35c",
    accent="#ff8a1f",
    foreground="#f2ede4",
    background="#0a0a0a",
    surface="#121212",
    panel="#1a1a1a",
    warning="#ffd23f",
    error="#ff4f3a",
    success="#7ddc6a",
    dark=True,
)
SORT_LABELS = {"score": "SCORE", "profit": "PROFIT", "profit_per_hour": "PROFIT/HR"}
COLUMNS = [("#", 2), ("COMMODITY", 13), ("BUY AT", 15), ("SELL AT", 15), ("QTY", 3), ("INVEST", 6),
           ("PROFIT", 7), ("MRG", 4), ("ROI", 4), ("GM", 4), ("ETA", 4), ("P/HR", 6), ("AGE", 4),
           ("CONF", 4), ("FLAGS", 9)]
PLACE_COLUMNS = ("BUY AT", "SELL AT")
TAG_WIDTH = 5   # "STAN " in front of each place when the table mixes systems
MAX_LOOKUPS_PER_RUN = 60


class TradeApp(App):
    CSS_PATH = "theme.tcss"
    TITLE = "Stanton Trade Optimizer"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("w", "wallet", "Wallet"),
        Binding("s", "ship", "Ship"),
        Binding("l", "illegal", "Illegal"),
        Binding("o", "sort", "Sort"),
        Binding("y", "systems", "Systems"),
        Binding("m", "mixed", "Mixed"),
        Binding("x", "jumps", "Jumps"),
        Binding("c", "calibrate", "Calibrate"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config, service: DataService, store: Store, *,
                 clock: Callable[[], float] = time.time, start_ocr: bool = True, sync_factory=None,
                 terminal_factory=None):
        super().__init__()
        self.cfg = config
        self.svc = service
        self.store = store
        self.clock = clock
        self.start_ocr = start_ocr
        self.sync_factory = sync_factory
        self.sync = None
        self.ocr_status = "OCR off"
        self.terminal_factory = terminal_factory
        self.watcher = None
        self.terminal_status = ""
        self.references: dict[str, float] = {}

        saved = store.get_state("wallet")
        if saved and saved.get("source") in ("ocr", "manual"):
            self.wallet, self.wallet_source, self.wallet_at = int(saved["value"]), saved["source"], saved.get("at")
        else:
            self.wallet, self.wallet_source, self.wallet_at = int(config.get("wallet", "balance", 0)), "config", None
        self.sanity = WalletSanity(float(config.get("ocr", "big_change_pct", 60)),
                                   self.wallet if self.wallet_source != "config" else None)

        self.ship = config.active_ship()
        self.include_illegal = bool(config.get("optimizer", "include_illegal", False))
        self.sort = str(config.get("optimizer", "sort", "score"))
        if self.sort not in SORT_MODES:
            self.sort = "score"
        self.mixed = bool(config.get("optimizer", "mixed_cargo", False))
        self.cross = bool(config.get("optimizer", "cross_system", True))
        configured = [s for s in config.get("data", "systems", ["Stanton"])]
        self.scopes: list[list[str]] = [["Stanton"], ["Pyro"], ["Nyx"], ["Stanton", "Pyro", "Nyx"]]
        if configured not in self.scopes:
            self.scopes.insert(0, configured)
        self.scope_idx = self.scopes.index(configured)

        self.ds: Dataset | None = None
        self.routes: list[Route] = []
        self._columns_tagged = False
        self.loading = True
        self.refreshing = False
        self.last_errors: list[str] = []
        self._lookup_lock = threading.Lock()
        self._pending_pairs: list[tuple[int, int]] = []
        self._looked_up: set[tuple[int, int]] = set()
        self._gap_filled: set[int] = set()

    # layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(wordmark_markup(), id="wordmark")
            yield Static("", id="status")
        yield Static("", id="banner")
        yield DataTable(id="routes", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="detail"):
            yield Static("", id="buy")
            yield Static("", id="sell")
            yield Static("", id="run")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(PHOSPHOR)
        self.theme = PHOSPHOR.name
        self.set_columns(tagged=False)
        for pane, title in (("#buy", "BUY"), ("#sell", "SELL"), ("#run", "RUN")):
            self.query_one(pane).border_title = f"▌{title}"
        self.update_status()
        self.load_cached()
        self.set_interval(1, self.update_status)
        self.set_interval(20, self.tick_refresh)
        if self.start_ocr and bool(self.cfg.get("ocr", "enabled", True)):
            self.start_wallet_sync()
        if self.start_ocr and bool(self.cfg.get("terminal", "enabled", True)):
            self.start_terminal_watch()

    # A default console window is ~120x30, smaller than the full layout. Drop the
    # wordmark when narrow and the detail pane when short; the table always stays.
    WORDMARK_MIN_WIDTH = 132
    DETAIL_MIN_HEIGHT = 38

    def on_resize(self, event) -> None:
        self.fit_layout(event.size.width, event.size.height)

    def fit_layout(self, width: int, height: int) -> None:
        self.query_one("#wordmark").display = width >= self.WORDMARK_MIN_WIDTH
        self.query_one("#detail").display = height >= self.DETAIL_MIN_HEIGHT

    # data

    # Background work never takes the app down: an unexpected error is logged
    # to logs/app.log and shown, and the app keeps ranking from the cache.

    def _background_error(self, what: str, error: Exception) -> str:
        app_log().exception("%s failed", what, exc_info=error)
        return f"{what} failed: {error!r}"[:300]

    @work(thread=True, exclusive=True, group="load", exit_on_error=False)
    def load_cached(self) -> None:
        try:
            ds = self.svc.dataset(self.cfg.pad_overrides())
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, self._background_error("Loading the cache", e), severity="error")
            ds = None
        self.call_from_thread(self._dataset_ready, ds)
        self.call_from_thread(self.refresh_data)

    def tick_refresh(self) -> None:
        if not self.refreshing:
            self.refresh_data()

    @work(thread=True, exclusive=True, group="refresh", exit_on_error=False)
    def refresh_data(self, prices_now: bool = False) -> None:
        self.refreshing = True
        ds = None
        try:
            errors = self.svc.refresh(force_prices=prices_now)
            ds = self.svc.dataset(self.cfg.pad_overrides()) if (self.svc.fetched or self.ds is None) else None
        except Exception as e:  # noqa: BLE001
            errors = [self._background_error("Refresh", e)]
        finally:
            self.refreshing = False
        self.call_from_thread(self._refreshed, ds, errors, prices_now)

    def _refreshed(self, ds: Dataset | None, errors: list[str], manual: bool) -> None:
        self.loading = False
        self.last_errors = errors
        if ds is not None:
            self._dataset_ready(ds)
        if manual:
            if errors:
                self.notify(errors[0], title="Refresh failed", severity="error")
            else:
                self.notify("Prices updated from UEX.", title="Refresh")
        self.update_status()

    def _dataset_ready(self, ds: Dataset | None) -> None:
        if ds is None:
            return
        from ..captures import commodity_references

        self.references = commodity_references(ds)
        self.ds = ds
        self.recompute()
        self.fill_distance_gaps()

    def settings(self) -> Settings:
        g = self.cfg.get
        ids = frozenset(i for i in (self.ds.system_id(n) for n in self.scopes[self.scope_idx]) if i is not None)
        return Settings(
            wallet=self.wallet, reserve=float(g("wallet", "reserve", 150_000)), ship=self.ship, now=self.clock(),
            system_ids=ids, include_illegal=self.include_illegal, sort=self.sort,
            top_n=int(g("optimizer", "top_n", 15)), mixed_cargo=self.mixed,
            overhead_min=float(g("optimizer", "overhead_min", 8)),
            confidence_full_h=float(g("optimizer", "confidence_full_h", 2)),
            confidence_half_life_h=float(g("optimizer", "confidence_half_life_h", 22)),
            unknown_demand_factor=float(g("optimizer", "unknown_demand_factor", 0.8)),
            same_orbit_gm=float(g("optimizer", "same_orbit_gm", 1.0)),
            show_unknown_pad=bool(g("optimizer", "show_unknown_pad", True)),
            cross_system=self.cross, jump_min=float(g("optimizer", "jump_min", 5)),
        )

    def recompute(self) -> None:
        if self.ds is None:
            return
        self.routes = find_routes(self.ds, self.settings())
        self.fill_table()
        self.queue_lookups([(r.buy_terminal.id, r.sell_terminal.id) for r in self.routes if r.leg.is_estimate])
        self.update_status()

    # distance lookups (one API call per pair, so they trickle in the background)

    def fill_distance_gaps(self) -> None:
        if self.ds is None:
            return
        for sid in self.settings().system_ids - self._gap_filled:
            self._gap_filled.add(sid)
            self.queue_lookups(missing_orbit_pairs(self.ds, {sid}))

    def queue_lookups(self, pairs: list[tuple[int, int]]) -> None:
        fresh = [p for p in pairs if p not in self._looked_up]
        if not fresh:
            return
        self._looked_up.update(fresh)
        with self._lookup_lock:
            self._pending_pairs.extend(fresh)
        self.run_lookups()

    @work(thread=True, exclusive=False, group="distances", exit_on_error=False)
    def run_lookups(self) -> None:
        if not self._lookup_lock.acquire(blocking=False):
            return
        try:
            todo, self._pending_pairs = self._pending_pairs[:MAX_LOOKUPS_PER_RUN], self._pending_pairs[MAX_LOOKUPS_PER_RUN:]
        finally:
            self._lookup_lock.release()
        if not todo:
            return
        try:
            saved = self.svc.lookup_distances(todo, limit=len(todo))
            ds = self.svc.dataset(self.cfg.pad_overrides()) if saved else None
        except Exception as e:  # noqa: BLE001 - distances are a nice-to-have; estimates stay
            self._background_error("Distance lookup", e)
            return
        if ds is not None:
            self.call_from_thread(self._dataset_ready, ds)

    # table and detail

    @property
    def tagged(self) -> bool:
        """Places carry a STAN / PYRO / NYX tag when the scope spans more than one system."""
        return len(self.scopes[self.scope_idx]) > 1

    def set_columns(self, tagged: bool) -> None:
        table = self.query_one("#routes", DataTable)
        table.clear(columns=True)
        for label, width in COLUMNS:
            width += TAG_WIDTH if tagged and label in PLACE_COLUMNS else 0
            table.add_column(label, width=width, key=label)
        self._columns_tagged = tagged

    def fill_table(self) -> None:
        table = self.query_one("#routes", DataTable)
        keep = table.cursor_row
        tagged = self.tagged
        if tagged != self._columns_tagged:
            self.set_columns(tagged)
        table.clear()
        for i, r in enumerate(self.routes, 1):
            name = r.commodity.name if len(r.cargo) == 1 else f"{r.commodity.name}+{len(r.cargo) - 1}"
            dist = f"{r.leg.gm:.0f}" + ("~" if r.leg.is_estimate else "")
            bt, st = r.buy_terminal, r.sell_terminal
            table.add_row(
                Text(str(i), style="dim"), name, fmt.place(bt.label, bt.system, tagged),
                fmt.place(st.label, st.system, tagged), str(r.qty),
                fmt.compact(r.investment), Text(fmt.compact(r.profit), style="bold #ff8a1f"),
                f"{r.margin_pct:.0f}%", f"{r.roi * 100:.0f}%", dist, f"{r.eta_min:.0f}m",
                fmt.compact(r.profit_per_hour), fmt.aged(r.age_h), fmt.conf_bar(r.confidence),
                fmt.flag_text(r.flags), key=str(i),
            )
        scope = " + ".join(self.scopes[self.scope_idx]).upper() + (" ▸ JUMPS" if tagged and self.cross else "")
        table.border_title = f"▌ROUTES ▸ {scope} ▸ {SORT_LABELS[self.sort]}"
        table.border_subtitle = f"{len(self.routes)} shown · ? for help"
        if self.routes:
            table.move_cursor(row=min(keep, len(self.routes) - 1))
        self.show_detail(table.cursor_row if self.routes else None)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_detail(event.cursor_row)

    def show_detail(self, index: int | None) -> None:
        buy, sell, run = self.query_one("#buy", Static), self.query_one("#sell", Static), self.query_one("#run", Static)
        if index is None or index >= len(self.routes):
            reserve = float(self.cfg.get("wallet", "reserve", 150_000))
            if self.ds is None:
                msg = "Loading UEX data..."
            elif self.wallet <= reserve:
                msg = (f"Wallet {self.wallet:,} is at or below the {reserve:,.0f} reserve.\n"
                       "Press w to enter your balance, or F1 in game with the mobiGlas region calibrated (c).")
            else:
                msg = "No profitable routes for this wallet, ship and scope."
            buy.update(msg)
            sell.update("")
            run.update("")
            return
        r = self.routes[index]
        now = self.clock()
        for pane, title, t in ((buy, "BUY", r.buy_terminal), (sell, "SELL", r.sell_terminal)):
            pane.border_title = Text.assemble(f"▌{title} ▸ ", fmt.place(t.label, t.system, self.tagged))
        buy.update(self._side(r, "buy", now))
        sell.update(self._side(r, "sell", now))
        run.update(self._run(r))

    def _side(self, r: Route, side: str, now: float) -> str:
        t = r.buy_terminal if side == "buy" else r.sell_terminal
        names = (self.ds.status_names.get(side, {}) if self.ds else {})
        lines = [f"[dim]{t.system} › {t.orbit} › {t.place}[/]"]
        for c in r.cargo:
            p = c.buy if side == "buy" else c.sell
            price = p.price_buy if side == "buy" else p.price_sell
            age_h = (now - p.updated) / 3600
            color = fmt.age_color(age_h)
            mine = {"you": "  [b #7ddc6a]★ yours[/]", "stt": f"  [b {fmt.STT}]◆ SC Trade Tools[/]"}.get(p.source, "")
            lines.append(f"[b]{c.commodity.name}[/]  {price:,.0f}/SCU  [{color}]{fmt.clock(p.updated)} · "
                         f"{fmt.age_text(age_h)} ago[/]{mine}")
            status = names.get(p.status_buy if side == "buy" else p.status_sell, "")
            if side == "buy":
                lines.append(f"  stock {fmt.full(c.stock)} SCU  [dim]{status}[/]")
            else:
                demand = f"{c.demand:,} SCU" if c.demand is not None else "[#ffd23f]not reported[/]"
                lines.append(f"  demand {demand}  [dim]holds {p.scu_sell_stock:,} · {status}[/]")
            boxes = ",".join(map(str, p.container_sizes)) if p.container_sizes else "[#ffd23f]unknown[/]"
            lines.append(f"  boxes {boxes}")
        pads = ",".join(sorted(t.pad_types, key=lambda p: PAD_ORDER.get(p, 9))) if t.pad_types else ""
        pad = (f"{pads or 'none'}" + (" (yours)" if t.pad_source == "override" else "")) if t.pad_types is not None \
            else "[#ffd23f]unknown[/]"
        dock = " ".join(n for n, v in (("dock-port", t.has_docking_port), ("loading-dock", t.has_loading_dock),
                                       ("freight-elev", t.has_freight_elevator), ("auto-load", t.is_auto_load)) if v)
        lines.append(f"pad {pad}  [dim]{dock}[/]")
        return "\n".join(lines)

    def _run(self, r: Route) -> str:
        reserve = float(self.cfg.get("wallet", "reserve", 150_000))
        after = self.wallet + r.profit
        dist = f"{r.leg.gm:.1f} Gm" + (" (est.)" if r.leg.is_estimate else f" ({r.leg.quality})")
        lines = [
            f"qty      {r.qty} / {self.ship.cargo_scu} SCU",
            f"invest   {r.investment:,.0f}",
            f"fuel     ~{r.fuel_cost:,.0f}",
            f"[b #ff8a1f]profit   {r.profit:,.0f}[/]",
            f"roi      {r.roi * 100:.1f}%   conf {r.confidence * 100:.0f}%",
            f"dist     {dist}",
            f"eta      {r.eta_min:.1f} min · {fmt.compact(r.profit_per_hour)}/h",
            *([f"jump     {r.buy_terminal.system} → {r.sell_terminal.system} via {r.via} "
               f"[dim](+{float(self.cfg.get('optimizer', 'jump_min', 5)):.0f} min)[/]"] if r.via else []),
            f"[b]wallet → {after:,.0f}[/]",
        ]
        if after - reserve < 0:
            lines.append("[#ff4f3a]below reserve[/]")
        return "\n".join(lines)

    # header

    def update_status(self) -> None:
        now = self.clock()
        reserve = float(self.cfg.get("wallet", "reserve", 150_000))
        src = {"ocr": "OCR", "manual": "MANUAL", "config": "CONFIG"}[self.wallet_source]
        when = fmt.clock(self.wallet_at) if self.wallet_at else "default"
        f = self.svc.freshness()
        if f.prices_at:
            data = f"prices {fmt.clock(f.prices_at)} ({fmt.age_text((now - f.prices_at) / 3600)} ago)"
        else:
            data = "prices: none yet"
        nxt = fmt.countdown(f.next_refresh_at - now)
        live = "[#ff4f3a]● STALE[/]" if f.stale_since else ("[#ffd23f]● LOADING[/]" if self.loading else "[#7ddc6a]● LIVE[/]")
        scope = " + ".join(self.scopes[self.scope_idx]).upper()
        n_terms = sum(1 for t in self.ds.terminals.values() if t.system in self.scopes[self.scope_idx]) if self.ds else 0
        o = lambda flag: "[b]ON[/]" if flag else "[dim]OFF[/]"  # noqa: E731
        s = self.ship
        self.query_one("#status", Static).update("\n".join([
            "",
            f"[$text-muted]WALLET[/]  [b #ff8a1f]{self.wallet:,} aUEC[/]  [dim]{src} {when}[/]"
            f"    [$text-muted]RESERVE[/] {reserve:,.0f}    [$text-muted]BUDGET[/] [b]{max(self.wallet - reserve, 0):,.0f}[/]",
            f"[$text-muted]SHIP[/]    [b]{s.name}[/]  [dim]{s.cargo_scu} SCU · box ≤{s.max_container_scu} · pad {s.pad}[/]",
            f"[$text-muted]SCOPE[/]   [b]{scope}[/]  [dim]{n_terms} terminals[/]    [$text-muted]SORT[/] [b]{SORT_LABELS[self.sort]}[/]"
            f"    [$text-muted]ILLEGAL[/] {o(self.include_illegal)}    [$text-muted]MIXED[/] {o(self.mixed)}"
            f"    [$text-muted]JUMPS[/] {o(self.cross)}",
            f"[$text-muted]DATA[/]    {live}  {data}  [dim]· next {nxt}[/]{self._sctrade_text(now)}",
            f"[$text-muted]OCR[/]     {self.ocr_status}"
            + (f"    [$text-muted]TERMINAL[/] {self.terminal_status}" if self.terminal_status else ""),
        ]))
        banner = self.query_one("#banner", Static)
        if f.stale_since:
            banner.update(f"▲ STALE SINCE {fmt.clock(f.stale_since)}: UEX unreachable, showing cached prices. "
                          f"{(f.last_error or '')[:90]}")
            banner.set_classes("show")
        elif self.ds is None and self.last_errors:
            banner.update(f"▲ NO DATA: {self.last_errors[0][:110]}")
            banner.set_classes("show")
        else:
            banner.set_classes("")

    def _sctrade_text(self, now: float) -> str:
        meta = self.svc.sctrade_status()
        if meta is None:
            return ""
        failed = meta["last_error"] and (meta["fetched_at"] is None or meta["last_attempt"] >= meta["fetched_at"])
        if failed:
            state = "[#ffd23f]unreachable[/]"
        elif meta["fetched_at"] is None:
            state = "[dim]waiting[/]"
        else:
            used = self.ds.sctrade_used if self.ds else 0
            state = f"{used} newer than UEX [dim]{fmt.clock(meta['fetched_at'])}[/]"
        return f"    [$text-muted]SC TRADE[/] [{fmt.STT}]◆[/] {state}"

    # wallet

    def set_wallet(self, value: int, source: str, at: float | None = None) -> None:
        self.wallet, self.wallet_source, self.wallet_at = int(value), source, at or self.clock()
        self.store.set_state("wallet", {"value": self.wallet, "source": source, "at": self.wallet_at})
        if source == "manual":
            self.sanity.reset(self.wallet)
        self.recompute()

    def start_wallet_sync(self) -> None:
        from ..ocr.sync import WalletSync

        factory = self.sync_factory or WalletSync
        g = self.cfg.get
        self.sync = factory(
            region=self.cfg.ocr_region, sanity=self.sanity,
            on_read=lambda read: self.call_from_thread(self.on_ocr_read, read),
            on_region_moved=lambda monitor, region: self.call_from_thread(self.region_moved, monitor, region),
            hotkey=str(g("ocr", "hotkey", "f1")), delay_s=float(g("ocr", "delay_s", 1.2)),
            engine_kind=str(g("ocr", "engine", "auto")), tesseract_cmd=str(g("ocr", "tesseract_cmd", "")),
        )
        try:
            self.sync.start()
        except Exception as e:  # noqa: BLE001
            self.ocr_status = f"[#ff4f3a]hotkey unavailable: {e}[/]"
            return
        key = str(g("ocr", "hotkey", "f1")).upper()
        calibrated = self.cfg.ocr_region() is not None
        self.ocr_status = f"[#7ddc6a]●[/] {key} ready" if calibrated else f"[#ffd23f]●[/] {key} armed, region not set: press c"

    def on_ocr_read(self, read) -> None:
        when = fmt.clock(read.at)
        if read.accepted:
            self.ocr_status = f"[#7ddc6a]●[/] read {read.value:,} at {when}"
            self.set_wallet(read.value, "ocr", read.at)
            self.notify(f"{read.value:,} aUEC", title="Wallet (OCR)", timeout=3)
        elif read.value is not None:
            self.ocr_status = f"[#ffd23f]●[/] {read.value:,} at {when}: {read.reason}"
        elif read.reason != "not a number":
            self.ocr_status = f"[#ff4f3a]●[/] {read.reason}"
        self.update_status()

    # actions

    def action_refresh(self) -> None:
        self.loading = True
        self.update_status()
        self.refresh_data(prices_now=True)

    def action_wallet(self) -> None:
        def done(value):
            if value is not None:
                self.set_wallet(value, "manual")
                self.notify(f"{value:,} aUEC", title="Wallet (manual)", timeout=3)
        self.push_screen(WalletScreen(), done)

    def action_ship(self) -> None:
        ships = self.cfg.ships()

        def done(choice):
            if not choice:
                return
            kind, name = choice
            if kind == "use" and name in ships:
                self.use_ship(name)
            elif kind == "add":
                self.open_add_ship()
            elif kind == "remove":
                self.cfg.remove_ship(name)
                self.notify(f"Removed {name}.", title="Ships")
                self.action_ship()
        self.push_screen(ShipScreen(ships, self.ship.name), done)

    def use_ship(self, name: str) -> None:
        self.ship = self.cfg.ships()[name]
        self.cfg.set_active_ship(name)
        self.recompute()
        self.notify(f"{name}: {self.ship.cargo_scu:,} SCU, box ≤{self.ship.max_container_scu}, pad {self.ship.pad or '?'}",
                    title="Ship")

    # adding ships from the UEX list (cached; refreshed daily or with Ctrl+R)

    def open_add_ship(self) -> None:
        cached_at = self.store.meta("vehicles")["fetched_at"]
        screen = AddShipScreen(self.svc.cargo_ships(), set(self.cfg.ships()), cached_at)
        self.push_screen(screen, self.add_ship_chosen)
        if not screen.ships:
            self.refresh_ship_list(screen)

    @work(thread=True, exclusive=True, group="ships", exit_on_error=False)
    def refresh_ship_list(self, screen: AddShipScreen) -> None:
        try:
            error = self.svc.refresh_vehicles()
            ships = self.svc.cargo_ships()
        except Exception as e:  # noqa: BLE001
            error, ships = self._background_error("Ship list refresh", e), []
        cached_at = self.store.meta("vehicles")["fetched_at"]
        if error:
            self.call_from_thread(self.notify, error, title="Ship list", severity="error")
        if ships:
            self.call_from_thread(screen.set_ships, ships, cached_at)

    def add_ship_chosen(self, vehicle: dict | None) -> None:
        if vehicle is None:
            return
        name = str(vehicle["name_full"])
        if name in self.cfg.ships():
            self.use_ship(name)
            return
        box = vehicle_max_box(vehicle)
        pad = str(vehicle.get("pad_type") or "").upper()

        def finish(max_box: int, pad_size: str) -> None:
            self.cfg.add_ship(ShipProfile.from_vehicle(vehicle, max_box, pad_size))
            self.use_ship(name)

        def ask_pad(max_box: int) -> None:
            if pad:
                finish(max_box, pad)
                return
            choices = [(f"{p} pad", p) for p in PAD_ORDER] + [("Don't know (rows show P?)", "")]
            self.push_screen(ChoiceScreen("PAD SIZE", f"UEX doesn't list a pad size for the {name}.",
                                          choices, default=len(choices) - 1),
                             lambda p: finish(max_box, p) if p is not None else None)

        if box is not None:
            ask_pad(box)
            return
        sizes = [32, 24, 16, 8, 4, 2, 1]
        guess = next((i for i, s in enumerate(sizes) if s <= int(vehicle.get("scu") or 0)), len(sizes) - 1)
        self.push_screen(ChoiceScreen("BIGGEST BOX", f"UEX doesn't list container sizes for the {name}.\n"
                                      "Biggest box (SCU) its hold takes:",
                                      [(f"{s} SCU", s) for s in sizes], default=guess),
                         lambda b: ask_pad(b) if b is not None else None)

    def action_illegal(self) -> None:
        self.include_illegal = not self.include_illegal
        self.recompute()

    def action_sort(self) -> None:
        self.sort = SORT_MODES[(SORT_MODES.index(self.sort) + 1) % len(SORT_MODES)]
        self.recompute()

    def action_systems(self) -> None:
        self.scope_idx = (self.scope_idx + 1) % len(self.scopes)
        self.recompute()
        self.fill_distance_gaps()

    def action_mixed(self) -> None:
        self.mixed = not self.mixed
        self.recompute()

    def action_jumps(self) -> None:
        self.cross = not self.cross
        if not self.tagged:
            self.notify("Jumps only matter with more than one system on: press y.", title="Jumps")
        self.recompute()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_calibrate(self) -> None:
        from ..ocr import capture
        from ..ocr.calibrate import newest_screenshot

        try:
            mons = capture.monitors()
        except Exception:  # noqa: BLE001
            mons = []
        where = self.cfg.ocr_region()
        current = f"monitor {where[0]}, box {where[1]}" if where else "not set"
        monitor = where[0] if where else 1
        shot = newest_screenshot()

        def done(choice):
            if not choice:
                return
            kind, value = choice
            if kind == "auto":
                self.auto_calibrate(monitor)
            elif kind == "file":
                self.calibrate_from_file(value, monitor)
            else:
                self.cfg.set_ocr_region(*value)
                self.notify(f"Saved monitor {value[0]}, box {value[1]}. Test read...", title="Calibrate")
                self.test_read()
        self.push_screen(CalibrateScreen(current, mons, monitor, str(shot) if shot else ""), done)

    def ocr_engine(self):
        if self.sync is not None:
            return self.sync.engine()
        from ..ocr.engine import make_engine

        return make_engine(str(self.cfg.get("ocr", "engine", "auto")), str(self.cfg.get("ocr", "tesseract_cmd", "")))

    @work(thread=True, exclusive=True, group="calibrate", exit_on_error=False)
    def auto_calibrate(self, monitor: int) -> None:
        """No dragging: the game keeps the mouse. Screenshot each monitor and
        find the balance on the open mobiGlas by OCR."""
        from ..ocr.calibrate import calibrate_screen

        for left in range(5, 0, -1):
            self.call_from_thread(self.notify, f"Switch to the game and press F1. Looking in {left}s.",
                                  title="Calibrate", timeout=1.2)
            time.sleep(1)
        try:
            cal = calibrate_screen(self.ocr_engine(), preferred=monitor)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.calibrated, None, str(e), True)
            return
        self.call_from_thread(self.calibrated, cal, "No open mobiGlas found on any monitor. Press c, then A, "
                              "and open it with F1 within 5 s (or use F with a screenshot).", True)

    @work(thread=True, exclusive=True, group="calibrate", exit_on_error=False)
    def calibrate_from_file(self, path: str, monitor: int) -> None:
        from ..ocr.calibrate import calibrate_image

        try:
            cal = calibrate_image(path, self.ocr_engine(), preferred=monitor)
        except Exception as e:  # noqa: BLE001 - missing file, wrong shape
            self.call_from_thread(self.calibrated, None, str(e), False)
            return
        self.call_from_thread(self.calibrated, cal, "No mobiGlas app bar found in that screenshot.", False)

    def calibrated(self, cal, failure: str, live: bool) -> None:
        if cal is None:
            self.notify(failure, title="Calibrate", severity="warning", timeout=10)
            return
        self.cfg.set_ocr_region(cal.monitor, cal.region)
        self.notify(f"Found {cal.value:,} on {cal.source}. Saved monitor {cal.monitor}, box {cal.region}. "
                    "F1 now syncs your wallet.", title="Calibrate", timeout=10)
        if live and self.sanity.offer(cal.value)[0]:
            self.set_wallet(cal.value, "ocr")
        self.start_or_update_ocr_status()

    def region_moved(self, monitor: int, region: tuple[int, int, int, int]) -> None:
        self.cfg.set_ocr_region(monitor, region)
        self.notify("The balance sat a little differently; OCR box re-centred.", title="OCR", timeout=3)

    @work(thread=True, exclusive=True, group="calibrate", exit_on_error=False)
    def test_read(self) -> None:
        if self.sync is None:
            self.call_from_thread(self.notify, "OCR is off in config.toml.", title="Test read")
            return
        try:
            monitor, box = self.cfg.ocr_region()
            read = self.sync.read_image(self.sync.grab(monitor, box), apply=False)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, str(e), title="Test read", severity="error")
            return
        if read.value is not None:
            msg, sev = f"Read {read.value:,}. Press F1 in game to sync for real.", "information"
        else:
            msg, sev = f"No number found (saw: {read.text[:80]!r}). Try c, then A.", "warning"
        self.call_from_thread(self.notify, msg, title="Test read", severity=sev, timeout=8)
        self.call_from_thread(self.start_or_update_ocr_status)

    def start_or_update_ocr_status(self) -> None:
        key = str(self.cfg.get("ocr", "hotkey", "f1")).upper()
        if self.sync is not None and self.cfg.ocr_region():
            self.ocr_status = f"[#7ddc6a]●[/] {key} ready"
        self.update_status()

    def on_unmount(self) -> None:
        if self.sync is not None:
            self.sync.stop()
        if self.watcher is not None:
            self.watcher.stop()

    # commodity terminals: read automatically while one is on screen

    def terminal_context(self) -> tuple[list[str], dict[str, float]]:
        """Called from the watcher thread: commodity names and typical prices."""
        ds = self.ds
        return ([c.name for c in ds.commodities.values()], self.references) if ds else ([], {})

    def start_terminal_watch(self) -> None:
        from ..ocr.terminal_watch import TerminalWatcher

        g = self.cfg.get
        factory = self.terminal_factory or TerminalWatcher
        self.watcher = factory(
            context=self.terminal_context,
            on_screen=lambda screen: self.call_from_thread(self.terminal_seen, screen),
            interval_s=float(g("terminal", "interval_s", 3)),
            repeat_s=float(g("terminal", "repeat_s", 60)),
            window_title=str(g("terminal", "window_title", "Star Citizen")),
            engine_kind=str(g("ocr", "engine", "auto")), tesseract_cmd=str(g("ocr", "tesseract_cmd", "")),
        )
        self.watcher.start()
        self.terminal_status = "[#7ddc6a]●[/] watching"

    def terminal_seen(self, screen) -> None:
        self.terminal_status = "[#ffd23f]●[/] reading terminal..."
        self.update_status()
        self.save_terminal_capture(screen)

    @work(thread=True, exclusive=False, group="captures", exit_on_error=False)
    def save_terminal_capture(self, screen) -> None:
        from ..captures import capture_rows, identify_terminal

        try:
            ds = self.ds
            if ds is None:
                return
            found = identify_terminal(ds, screen)
            if found.terminal_id is None:
                guess = ", ".join(found.candidates) or "none"
                self.call_from_thread(self.capture_done, None, 0, f"couldn't tell which terminal (best: {guess})",
                                      screen.balance)
                return
            rows = capture_rows(ds, screen, found.terminal_id, self.clock())
            self.store.save_captures(rows)
            fresh = self.svc.dataset(self.cfg.pad_overrides())
            label = ds.terminals[found.terminal_id].label
            self.call_from_thread(self.capture_done, fresh, len(rows), label, screen.balance)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.capture_done, None, 0, self._background_error("Terminal capture", e), None)

    def capture_done(self, ds: Dataset | None, count: int, where: str, balance: int | None) -> None:
        when = fmt.clock(self.clock())
        if ds is not None:
            self._dataset_ready(ds)
            self.terminal_status = f"[#7ddc6a]●[/] {count} prices at {where} {when}"
            self.notify(f"{count} prices read at {where}.", title="Terminal", timeout=4)
        else:
            self.terminal_status = f"[#ffd23f]●[/] {where}"
        if balance and self.sanity.offer(balance)[0]:
            self.set_wallet(balance, "ocr")
        self.update_status()
