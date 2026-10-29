"""Modal screens: wallet entry, ship picker, calibration menu, help."""
from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..config import vehicle_max_box
from ..ocr.parse import parse_balance


class Dialog(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]
    DEFAULT_CSS = """
    Dialog { align: center middle; background: $background 70%; }
    Dialog > Vertical {
        width: 72; height: auto; max-height: 90%;
        border: heavy $primary; background: $surface; padding: 1 2;
    }
    Dialog .title { color: $primary; text-style: bold; margin-bottom: 1; }
    Dialog .hint { color: $text-muted; margin-top: 1; }
    Dialog .error { color: $error; }
    """


class WalletScreen(Dialog):
    """Returns the entered balance, or None."""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("SET WALLET", classes="title")
            yield Label("Current aUEC balance. Commas are fine.")
            yield Input(placeholder="1,234,567", id="wallet")
            yield Label("", id="err", classes="error")
            yield Label("Enter to save   Esc to cancel", classes="hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = parse_balance(event.value.replace("aUEC", ""))
        if value is None:
            self.query_one("#err", Label).update("That isn't a number.")
            return
        self.dismiss(value)


class ShipScreen(Dialog):
    """Your profiles. Returns ("use", name), ("add", None), ("remove", name) or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close"), Binding("a", "add", "Add"),
                Binding("delete", "remove", "Remove"), Binding("x", "remove", "Remove", show=False)]
    DEFAULT_CSS = "ShipScreen > Vertical { width: 84; }"

    def __init__(self, ships: dict, active: str):
        super().__init__()
        self.ships = ships
        self.names = list(ships)
        self.active = active

    def compose(self) -> ComposeResult:
        def line(name: str) -> Text:
            s = self.ships[name]
            t = Text()
            t.append("▶ " if name == self.active else "  ", style="bold #ff8a1f")
            t.append(f"{name:<36}", style="bold" if name == self.active else "")
            t.append(f" {s.cargo_scu:>6,} SCU  box {s.max_container_scu:>2}  pad {s.pad or '?'}", style="dim")
            return t

        with Vertical():
            yield Label("SHIPS", classes="title")
            yield OptionList(*[Option(line(n), id=n) for n in self.names], id="ships")
            yield Label("", id="err", classes="error")
            yield Label("Enter switch   A add a ship from UEX   Del remove   Esc close", classes="hint")

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        if self.active in self.names:
            ol.highlighted = self.names.index(self.active)
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(("use", event.option.id))

    def action_add(self) -> None:
        self.dismiss(("add", None))

    def action_remove(self) -> None:
        ol = self.query_one(OptionList)
        if ol.highlighted is None:
            return
        name = self.names[ol.highlighted]
        if name == self.active:
            self.query_one("#err", Label).update("That's the ship you're flying. Switch first, then remove it.")
            return
        self.dismiss(("remove", name))


def vehicle_line(v: dict, owned: bool) -> Text:
    box = vehicle_max_box(v)
    t = Text()
    t.append("✓ " if owned else "  ", style="#7ddc6a")
    t.append(f"{str(v.get('name_full', ''))[:38]:<38}", style="bold" if owned else "")
    t.append(f" {int(v.get('scu') or 0):>6,} SCU", style="#ff8a1f")
    t.append(f"  box {box if box else '?':>2}  pad {v.get('pad_type') or '?':<2}", style="dim")
    return t


class AddShipScreen(ModalScreen):
    """Searchable list of UEX cargo ships. Returns the chosen vehicles row, or None."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Back"),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("pagedown", "move(10)", show=False, priority=True),
        Binding("pageup", "move(-10)", show=False, priority=True),
        Binding("ctrl+r", "refresh_list", "Refresh list"),
    ]
    DEFAULT_CSS = """
    AddShipScreen { align: center middle; background: $background 70%; }
    AddShipScreen > Vertical {
        width: 80; height: 85%; border: heavy $primary; background: $surface; padding: 1 2;
    }
    AddShipScreen .title { color: $primary; text-style: bold; }
    AddShipScreen #list { height: 1fr; margin-top: 1; }
    AddShipScreen .hint { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, ships: list[dict], owned: set[str], cached_at: float | None):
        super().__init__()
        self.ships = ships
        self.owned = owned
        self.cached_at = cached_at
        self.shown: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("ADD A SHIP", classes="title")
            yield Input(placeholder="type to filter: hull, drake, caterpillar, 696 ...", id="filter")
            yield OptionList(id="list")
            yield Label("", id="hint", classes="hint")

    def on_mount(self) -> None:
        self.fill()
        self.query_one("#filter", Input).focus()

    def fill(self) -> None:
        phrase = " ".join(self.query_one("#filter", Input).value.lower().split())
        words = phrase.split()

        def text(v: dict) -> str:
            return f"{v.get('name_full', '')} {v.get('company_name', '')} {v.get('scu', '')}".lower()

        matches = [v for v in self.ships if all(w in text(v) for w in words)]
        # Names containing the whole phrase first: "hull c" puts the Hull C above
        # the Hull A, whose "MISC" also contains a "c".
        self.shown = ([v for v in matches if phrase in str(v.get("name_full", "")).lower()]
                      + [v for v in matches if phrase not in str(v.get("name_full", "")).lower()])
        ol = self.query_one("#list", OptionList)
        ol.clear_options()
        ol.add_options([Option(vehicle_line(v, v.get("name_full") in self.owned), id=str(v["id"])) for v in self.shown])
        if self.shown:
            ol.highlighted = 0
        when = time.strftime("%b %d %H:%M", time.localtime(self.cached_at)) if self.cached_at else "not fetched yet"
        self.query_one("#hint", Label).update(
            f"{len(self.shown)} of {len(self.ships)} cargo ships · UEX list from {when} (concept ships hidden)\n"
            "↑↓ choose   Enter add and fly it   Ctrl+R refresh list from UEX   Esc back")

    def set_ships(self, ships: list[dict], cached_at: float | None) -> None:
        self.ships, self.cached_at = ships, cached_at
        self.fill()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.fill()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.choose()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.choose()

    def choose(self) -> None:
        ol = self.query_one("#list", OptionList)
        if ol.highlighted is not None and ol.highlighted < len(self.shown):
            self.dismiss(self.shown[ol.highlighted])

    def action_move(self, delta: int) -> None:
        ol = self.query_one("#list", OptionList)
        if self.shown:
            ol.highlighted = max(0, min(len(self.shown) - 1, (ol.highlighted or 0) + delta))

    def action_refresh_list(self) -> None:
        self.query_one("#hint", Label).update("Fetching the ship list from UEX ...")
        self.app.refresh_ship_list(self)


class ChoiceScreen(Dialog):
    """Pick one value from a short list. Returns the value, or None."""

    def __init__(self, title: str, message: str, choices: list[tuple[str, object]], default: int = 0):
        super().__init__()
        self.title_text = title
        self.message = message
        self.choices = choices
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, classes="title")
            yield Label(self.message)
            yield OptionList(*[Option(label, id=str(i)) for i, (label, _) in enumerate(self.choices)], id="choices")
            yield Label("Enter choose   Esc cancel", classes="hint")

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.highlighted = self.default
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.choices[int(event.option.id)][1])


class CalibrateScreen(Dialog):
    """Returns ("auto", None), ("file", path) or ("coords", (monitor, (x, y, w, h))), or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close"), Binding("a", "auto", "Auto"),
                Binding("f", "file", "From screenshot"), Binding("e", "coords", "Coordinates")]

    def __init__(self, current: str, monitors: list[dict], monitor: int, screenshot: str = ""):
        super().__init__()
        self.current = current
        self.monitors = monitors
        self.monitor = monitor
        self.screenshot = screenshot

    def compose(self) -> ComposeResult:
        mons = "  ".join(f"{i}: {m['width']}x{m['height']}" for i, m in enumerate(self.monitors) if i > 0)
        with Vertical():
            yield Label("CALIBRATE OCR REGION", classes="title")
            yield Static(f"Current: {self.current}\nMonitors: {mons or 'none found'}")
            yield Label("")
            yield Label("[b]A[/b]  auto-detect: you get 5 s to switch to the game and press F1. "
                        "The app finds the balance on the mobiGlas by itself. Nothing to click.")
            yield Label("")
            yield Label("[b]F[/b]  auto-detect from a full-screen screenshot with the mobiGlas open:")
            yield Input(value=self.screenshot, placeholder=r"C:\path\to\screenshot.jpg", id="file", disabled=True)
            yield Label("[b]E[/b]  enter coordinates: x y width height [monitor]")
            yield Input(placeholder="960 1290 230 50 1", id="coords", disabled=True)
            yield Label("", id="err", classes="error")

    def action_auto(self) -> None:
        if not isinstance(self.focused, Input):
            self.dismiss(("auto", None))

    def action_file(self) -> None:
        if not isinstance(self.focused, Input):
            box = self.query_one("#file", Input)
            box.disabled = False
            box.focus()

    def action_coords(self) -> None:
        if not isinstance(self.focused, Input):
            box = self.query_one("#coords", Input)
            box.disabled = False
            box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        err = self.query_one("#err", Label)
        if event.input.id == "file":
            path = event.value.strip().strip('"')
            if not path:
                err.update("Need a screenshot path.")
                return
            self.dismiss(("file", path))
            return
        parts = event.value.replace(",", " ").split()
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            nums = []
        if len(nums) not in (4, 5) or nums[2] <= 0 or nums[3] <= 0:
            err.update("Need: x y width height [monitor], whole numbers.")
            return
        monitor = nums[4] if len(nums) == 5 else self.monitor
        self.dismiss(("coords", (monitor, tuple(nums[:4]))))


HELP_TEXT = """\
[b $primary]KEYS[/]
  r  refresh prices now          w  enter wallet manually
  s  ships: switch, add from UEX, remove    l  toggle illegal commodities
  o  sort: score / profit / profit per hour
  y  systems: Stanton / Pyro / Nyx / all      m  mixed cargo
  c  calibrate the OCR region (auto-detects the balance)    q  quit
  F1 in game (mobiGlas) reads your balance by OCR

[b $primary]COLUMNS[/]
  AGE   oldest of the buy and sell price reports (green <6h, yellow <48h, red)
  CONF  confidence from age: full to 2h, half at 24h, ~0 after 3 days
  SCORE profit x confidence (the default ranking)

[b $primary]FLAGS[/]
  P?  pad size unknown at one end (UEX knows few; add [pad_overrides])
  B?  container sizes unknown at one end
  D?  sell terminal reports no demand: qty is not capped by it
  ~   distance estimated (same orbit or missing pair)
  S   stale: confidence under 25%

[b $primary]DATA[/]
  Crowdsourced UEX prices. Check the terminal before you buy.
  Travel time uses your ship's qt_speed_gm_s from config.toml.
"""


class HelpScreen(Dialog):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("HELP", classes="title")
            yield Static(HELP_TEXT)
            yield Label("Esc to close", classes="hint")
