"""Read a commodity terminal off the screen.

The terminal is a screen inside the game world, so where it lands on the
monitor depends on where the player stands. Everything is found from its
labels: the "COMMODITIES" title, "SHOP INVENTORY", "YOUR INVENTORIES" and
"CURRENT BALANCE".

Right panel (shop inventory, buy tab): one row per commodity the terminal
sells: name, inventory level, "SHOP QUANTITY <n> SCU", "¤<price>/SCU", and,
when one of your inventories is selected, "AVAILABLE CARGO SIZE" boxes.
Left panel (your inventories): the selected location's name, then "IN DEMAND"
rows for cargo you carry that this terminal buys: name, "<n> SCU", "¤<price>/SCU".

Read only: a screenshot in, numbers out.
"""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from PIL import Image, ImageOps

from .parse import CONFUSIONS, SEPARATORS

STATUS = {
    "OUT OF STOCK": 1, "VERY LOW INVENTORY": 2, "LOW INVENTORY": 3, "MEDIUM INVENTORY": 4,
    "HIGH INVENTORY": 5, "VERY HIGH INVENTORY": 6, "MAX INVENTORY": 7, "MAXIMUM INVENTORY": 7,
}
BOX_SIZES = {1, 2, 4, 8, 16, 24, 32}
NOISE = ("SHOP QUANTITY", "SHOP OUANTITY", "AVAILABLE CARGO", "IN STOCK", "QUALITY", "SHOP INVENTORY")
DETECT_SCALE = 0.5
READ_SCALES_TEXT_PX = (30, 24, 38)   # panel text heights to read at; results are merged


@dataclass(frozen=True)
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def up(self) -> str:
        return self.text.upper()

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def bottom(self) -> float:
        return self.y + self.h


@dataclass
class BuyRow:
    commodity: str
    price: float | None = None
    stock: int | None = None
    status: int | None = None
    boxes: tuple[int, ...] | None = None


@dataclass
class SellRow:
    commodity: str
    price: float | None = None
    cargo_scu: int | None = None


@dataclass
class TerminalScreen:
    buy: list[BuyRow] = field(default_factory=list)
    sell: list[SellRow] = field(default_factory=list)
    location: str | None = None
    balance: int | None = None

    def signature(self) -> tuple:
        return (self.location, tuple((r.commodity, r.price, r.stock) for r in self.buy),
                tuple((r.commodity, r.price) for r in self.sell))


# --- OCR helpers ---------------------------------------------------------------

def ocr_lines(image: Image.Image, engine, scale: float = 1.0, offset=(0, 0)) -> list[Line]:
    if scale != 1:
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
    gray = ImageOps.autocontrast(ImageOps.grayscale(image)).convert("RGB")
    ox, oy = offset
    return [Line(t, x / scale + ox, y / scale + oy, w / scale, h / scale) for t, x, y, w, h in engine.lines(gray)]


def _find(lines: list[Line], *needles: str) -> Line | None:
    for needle in needles:
        for line in lines:
            if needle in line.up:
                return line
    return None


def detect(image: Image.Image, engine) -> dict[str, Line] | None:
    """Cheap check (half resolution): is a commodity terminal on screen? Returns
    the anchor lines found, or None."""
    lines = ocr_lines(image, engine, DETECT_SCALE)
    title = _find(lines, "COMMODITIES")
    if title is None:
        return None
    anchors = {"title": title}
    for key, needle in (("balance", "CURRENT BALANCE"), ("shop", "SHOP INVENTORY"), ("yours", "YOUR INVENTORIES"),
                        ("stock", "IN STOCK")):
        found = _find(lines, needle)
        if found:
            anchors[key] = found
    return anchors if len(anchors) >= 2 else None


# --- number parsing --------------------------------------------------------------

def _clean_number_text(text: str) -> str:
    text = text.replace("�", " ").replace("¤", " ")
    return re.sub(rf"\s*([{re.escape(SEPARATORS)}])\s*", r"\1", text).strip()


def number_candidates(text: str) -> list[str]:
    """Digit strings that look like a properly grouped number in the text. A
    reading that starts with a separator (", 536") lost its leading digit and
    yields nothing."""
    out = []
    for token in _clean_number_text(text).split():
        mapped = token.translate(CONFUSIONS)
        # OCR reads the ¤ sign as a letter ("Ä200", "A3,198") glued to the number.
        mapped = re.sub(rf"^[^\d{re.escape(SEPARATORS)}]+(?=\d)", "", mapped)
        if not mapped or mapped[0] in SEPARATORS:
            continue
        if not re.fullmatch(rf"[0-9{re.escape(SEPARATORS)}]+", mapped):
            continue
        groups = re.split(rf"[{re.escape(SEPARATORS)}]", mapped)
        if any(g == "" for g in groups) or any(len(g) % 3 for g in groups[1:]):
            continue
        out.append("".join(groups))
    return out


def price_from(text: str) -> str | None:
    """"¤3,563/SCU" -> "3563"; None unless the line is a per-SCU price."""
    up = text.upper()
    cut = re.search(r"/\s*[S5][C(][UIJL1.]", up) or re.search(r"/\s*SC", up)
    if not cut:
        return None
    found = number_candidates(text[: cut.start()])
    return found[-1] if found else None


def scu_from(text: str) -> str | None:
    """"90,000 SCU" -> "90000"; ignores per-SCU prices."""
    up = text.upper()
    if "/" in up or not re.search(r"S[C(][UIJL1.]|SCU", up):
        return None
    head = re.split(r"S[C(][UIJL1.]", text, flags=re.IGNORECASE)[0]   # keep case: "g" is 9, "G" is 6
    found = number_candidates(head)
    return found[-1] if found else None


def choose(readings: list[str], reference: float | None = None) -> float | None:
    """Pick one value from readings of the same number at different zooms.

    OCR either drops the first digit (the ¤ sign eats it: "536" for 4,536) or
    adds one (the ¤ read as a digit: "44536"). Both are about 10x off, so with
    a reference (the commodity's typical UEX price) the reading nearest it wins.
    Without one, a reading that is the tail of a longer one is taken to have
    lost its first digit, and the most common of the rest wins."""
    readings = [r for r in readings if r and int(r) > 0]
    if not readings:
        return None
    counts = Counter(readings)
    if reference and reference > 0:
        best = min(counts, key=lambda r: (round(abs(math.log(int(r) / reference)), 1), -counts[r]))
        return float(best)
    full = [r for r in counts if not any(o != r and o.endswith(r) for o in counts)]
    return float(max(full, key=lambda r: (counts[r], len(r))))


# --- names -------------------------------------------------------------------------

class Vocabulary:
    def __init__(self, commodity_names: list[str]):
        self.names = {n.upper(): n for n in commodity_names}
        self.keys = list(self.names)

    def commodity(self, text: str) -> str | None:
        up = re.sub(r"[^A-Z0-9' ]", "", text.upper().translate(str.maketrans("0", "O"))).strip()
        if len(up) < 3 or any(n in up for n in NOISE) or "INVENTORY" in up:
            return None
        if up in self.names:
            return self.names[up]
        match = difflib.get_close_matches(up, self.keys, n=1, cutoff=0.82)
        return self.names[match[0]] if match else None


QUALIFIERS = {"MAX": 7, "MAXIMUM": 7, "VERY HIGH": 6, "HIGH": 5, "MEDIUM": 4, "LOW": 3, "VERY LOW": 2}


def status_from(text: str) -> int | None:
    """Inventory level from "MEDIUM INVENTORY" and OCR variants of it. A bare
    "INVENTORY" (the qualifier wasn't read) gives None, not a guess."""
    up = re.sub(r"[^A-Z ]", "", text.upper().translate(str.maketrans("01", "OI"))).strip()
    if "OUT OF STOCK" in up or "OUTOFSTOCK" in up.replace(" ", ""):
        return 1
    qualifier = re.sub(r"\s*INVENT\w*$", "", up).strip()
    if len(qualifier) < 2:
        return None
    match = difflib.get_close_matches(qualifier, list(QUALIFIERS), n=1, cutoff=0.6)
    return QUALIFIERS[match[0]] if match else None


# --- panels -------------------------------------------------------------------------

def _rows(lines: list[Line], vocab: Vocabulary, name_right: float) -> list[tuple[str, list[Line]]]:
    """Group lines into rows, each starting at a commodity name in the left
    part of the panel. Two-line names ("AGRICULTURAL" / "SUPPLIES") are joined."""
    lines = sorted(lines, key=lambda l: (l.y, l.x))
    left = [l for l in lines if l.x < name_right]
    names: list[tuple[str, Line]] = []
    used: set[int] = set()
    for i, line in enumerate(left):
        if i in used:
            continue
        name = vocab.commodity(line.text)
        if name is None and i + 1 < len(left):
            nxt = left[i + 1]
            if 0 < nxt.y - line.y < 2.0 * line.h:
                name = vocab.commodity(f"{line.text} {nxt.text}")
                if name:
                    used.add(i + 1)
        if name:
            names.append((name, line))
    rows = []
    for k, (name, start) in enumerate(names):
        top = start.y - 0.4 * start.h
        bottom = names[k + 1][1].y - 0.4 * names[k + 1][1].h if k + 1 < len(names) else float("inf")
        rows.append((name, [l for l in lines if top <= l.y < bottom and l is not start]))
    return rows


def _read_buy(lines: list[Line], vocab: Vocabulary, x0: float, width: float) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, band in _rows(lines, vocab, name_right=x0 + 0.55 * width):
        row = out.setdefault(name, {"price": [], "stock": [], "status": [], "boxes": []})
        in_boxes = False
        for line in band:
            if (p := price_from(line.text)) is not None:
                row["price"].append(p)
            elif (s := scu_from(line.text)) is not None:
                row["stock"].append(s)
            elif "AVAILABLE" in line.up or "CARGO SIZE" in line.up:
                in_boxes = True
            elif "INVENTORY" in line.up or "STOCK" in line.up:
                if (st := status_from(line.text)) is not None:
                    row["status"].append(st)
            elif in_boxes:
                sizes = [int(t) for t in re.findall(r"\d+", line.text.translate(CONFUSIONS)) if int(t) in BOX_SIZES]
                if sizes:
                    row["boxes"].append(tuple(sorted(set(sizes))))
    return out


def _read_sell(lines: list[Line], vocab: Vocabulary, x0: float, width: float) -> dict[str, dict]:
    start = _find(lines, "IN DEMAND")
    if start is None:
        return {}
    stop = _find([l for l in lines if l.y > start.y], "NO DEMAND", "CANNOT SELL")
    section = [l for l in lines if start.bottom <= l.y < (stop.y if stop else float("inf"))]
    out: dict[str, dict] = {}
    for name, band in _rows(section, vocab, name_right=x0 + 0.62 * width):
        row = out.setdefault(name, {"price": [], "scu": []})
        for line in band:
            if (p := price_from(line.text)) is not None:
                row["price"].append(p)
            elif (s := scu_from(line.text)) is not None:
                row["scu"].append(s)
    return out


def _location(lines: list[Line], yours: Line | None) -> str | None:
    if yours is None:
        return None
    below = sorted((l for l in lines if yours.bottom <= l.y < yours.bottom + 4 * yours.h), key=lambda l: l.y)
    for line in below:
        up = line.up.strip()
        if up.startswith("SELECT") or "DEMAND" in up or len(up) < 3:
            continue
        return line.text.strip().title()
    return None


def _read_panel(image: Image.Image, box: tuple[int, int, int, int], engine, text_px: float) -> list[Line]:
    x0, y0, x1, y1 = (max(0, round(box[0])), max(0, round(box[1])),
                      min(image.width, round(box[2])), min(image.height, round(box[3])))
    if x1 - x0 < 20 or y1 - y0 < 20:
        return []
    crop = image.crop((x0, y0, x1, y1))
    passes = []
    for target in READ_SCALES_TEXT_PX:
        passes.append(ocr_lines(crop, engine, target / max(text_px, 1), offset=(x0, y0)))
    return passes


def _read_balance(image: Image.Image, label: Line, engine) -> int | None:
    """The small "¤1,277,842 AUEC" under CURRENT BALANCE reads unreliably, so
    try two crops at every zoom and accept a value only when two readings
    agree. A wrong wallet is worse than none: F1 on the mobiGlas reads it too."""
    from .capture import ocr_variants
    from .parse import parse_balance

    h = round(label.h)
    crops = [
        (label.x - 1.5 * h, label.bottom - 1.0 * h, label.x + label.w + 1.5 * h, label.bottom + 2.4 * h),
        (label.x - 1.0 * h, label.bottom, label.x + label.w + 1.0 * h, label.bottom + 1.4 * h),
    ]
    votes: Counter = Counter()
    for x0, y0, x1, y1 in crops:
        crop = image.crop((max(0, round(x0)), max(0, round(y0)), min(image.width, round(x1)),
                           min(image.height, round(y1))))
        gray = ImageOps.autocontrast(ImageOps.grayscale(crop))
        padded = Image.new("L", (gray.width + 2 * h, gray.height + 2 * h), 0)
        padded.paste(gray, (h, h))
        for variant in ocr_variants(padded.convert("RGB")):
            value = parse_balance(engine.read(variant))
            if value is not None and value >= 1000:
                votes[value] += 1
    if not votes:
        return None
    value, count = votes.most_common(1)[0]
    return value if count >= 2 else None


def read_screen(image: Image.Image, engine, commodity_names: list[str],
                references: dict[str, float] | None = None,
                anchors: dict[str, Line] | None = None) -> TerminalScreen | None:
    """Full read of a terminal screen, or None when no terminal is showing.
    Pass the anchors from detect() to skip running it twice."""
    image = image.convert("RGB")
    anchors = anchors or detect(image, engine)
    if anchors is None:
        return None
    full = ocr_lines(image, engine, 1.0)
    title = _find(full, "COMMODITIES") or anchors["title"]
    T = title.h                                     # the title is about 2.2x the panel text
    shop = _find(full, "SHOP INVENTORY") or anchors.get("shop")
    stock = _find(full, "IN STOCK") or anchors.get("stock")
    yours = _find(full, "YOUR INVENTORIES") or anchors.get("yours")
    balance_line = _find(full, "CURRENT BALANCE") or anchors.get("balance")
    text_px = T / 2.2
    bottom = title.y + 30 * T

    shop_x0 = (shop.x if shop else title.x + 36.5 * T) - 0.5 * T
    shop_x1 = (balance_line.x + balance_line.w + 3 * T) if balance_line else shop_x0 + 16 * T
    shop_y0 = (stock.bottom if stock else (shop.bottom + 3.5 * T if shop else title.y + 6 * T))
    left_x0 = (yours.x if yours else title.x) - 1.0 * T
    left_x1 = left_x0 + 15 * T
    left_y0 = (yours.y if yours else title.y + 3 * T) - 0.3 * T

    vocab = Vocabulary(commodity_names)
    references = references or {}
    screen = TerminalScreen()

    buy_merge: dict[str, dict] = {}
    for lines in _read_panel(image, (shop_x0, shop_y0, shop_x1, bottom), engine, text_px):
        for name, row in _read_buy(lines, vocab, shop_x0, shop_x1 - shop_x0).items():
            m = buy_merge.setdefault(name, {"price": [], "stock": [], "status": [], "boxes": []})
            for k in m:
                m[k] += row[k]
    for name, m in buy_merge.items():
        price = choose(m["price"], references.get(name))
        stock = choose(m["stock"])
        status = Counter(m["status"]).most_common(1)[0][0] if m["status"] else None
        boxes = Counter(m["boxes"]).most_common(1)[0][0] if m["boxes"] else None
        if price is not None or stock is not None:
            screen.buy.append(BuyRow(name, price, int(stock) if stock is not None else None, status, boxes))

    sell_merge: dict[str, dict] = {}
    location_votes: list[str] = []
    for lines in _read_panel(image, (left_x0, left_y0, left_x1, bottom), engine, text_px):
        yours_here = _find(lines, "YOUR INVENTORIES")
        if (loc := _location(lines, yours_here)) is not None:
            location_votes.append(loc)
        for name, row in _read_sell(lines, vocab, left_x0, left_x1 - left_x0).items():
            m = sell_merge.setdefault(name, {"price": [], "scu": []})
            m["price"] += row["price"]
            m["scu"] += row["scu"]
    for name, m in sell_merge.items():
        price = choose(m["price"], references.get(name))
        scu = choose(m["scu"])
        if price is not None:
            screen.sell.append(SellRow(name, price, int(scu) if scu is not None else None))
    if location_votes:
        screen.location = Counter(location_votes).most_common(1)[0][0]

    if balance_line is not None:
        screen.balance = _read_balance(image, balance_line, engine)
    return screen
