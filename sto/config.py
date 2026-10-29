"""config.toml loading and saving, ship profiles, and the .env token."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import tomlkit

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
DEFAULT_CONFIG_PATH = ROOT / "config.default.toml"
ENV_PATH = ROOT / ".env"
CACHE_DIR = ROOT / "cache"
LOG_DIR = ROOT / "logs"

SYSTEM_NAMES = ("Stanton", "Pyro", "Nyx")
SORT_MODES = ("score", "profit", "profit_per_hour")


@dataclass(frozen=True)
class ShipProfile:
    name: str
    cargo_scu: int
    max_container_scu: int
    pad: str
    qt_speed_gm_s: float = 0.2
    qt_spool_s: float = 10.0
    qt_fuel_scu_per_gm: float = 0.02
    uex_id: int | None = None

    @classmethod
    def from_table(cls, name: str, t: dict) -> "ShipProfile":
        return cls(
            name=name,
            cargo_scu=int(t["cargo_scu"]),
            max_container_scu=int(t["max_container_scu"]),
            pad=str(t.get("pad", "")).upper(),
            qt_speed_gm_s=float(t.get("qt_speed_gm_s", 0.2)),
            qt_spool_s=float(t.get("qt_spool_s", 10)),
            qt_fuel_scu_per_gm=float(t.get("qt_fuel_scu_per_gm", 0.02)),
            uex_id=int(t["uex_id"]) if t.get("uex_id") else None,
        )

    @classmethod
    def from_vehicle(cls, v: dict, max_box: int | None = None, pad: str | None = None) -> "ShipProfile":
        """A profile from a UEX vehicles row. max_box / pad fill in what UEX left blank."""
        box = max_box or vehicle_max_box(v)
        if box is None:
            raise ValueError(f"UEX has no container sizes for {v.get('name_full')}; pass max_box")
        return cls(name=str(v["name_full"]), cargo_scu=int(v.get("scu") or 0), max_container_scu=int(box),
                   pad=(pad if pad is not None else str(v.get("pad_type") or "")).upper(), uex_id=int(v["id"]))


def vehicle_max_box(v: dict) -> int | None:
    """Biggest container a UEX vehicle takes, or None when UEX doesn't say."""
    sizes = [int(s) for s in str(v.get("container_sizes") or "").split(",") if s.strip().isdigit()]
    return max(sizes) if sizes else None


class Config:
    """Wraps the tomlkit document so saves keep the user's comments."""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        if not path.exists():
            # First run: start from the shipped defaults. Updates replace
            # config.default.toml but never the user's config.toml.
            shutil.copyfile(DEFAULT_CONFIG_PATH, path)
        self.doc = tomlkit.parse(path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.write_text(tomlkit.dumps(self.doc), encoding="utf-8")

    def section(self, name: str) -> dict:
        return self.doc.get(name, {})

    def get(self, section: str, key: str, default=None):
        value = self.section(section).get(key, default)
        return value.unwrap() if hasattr(value, "unwrap") else value

    def set(self, section: str, key: str, value) -> None:
        if section not in self.doc:
            self.doc[section] = tomlkit.table()
        self.doc[section][key] = value

    # ships

    def ships(self) -> dict[str, ShipProfile]:
        table = self.doc.get("ships", {})
        return {name: ShipProfile.from_table(name, t.unwrap()) for name, t in table.items()}

    def active_ship(self) -> ShipProfile:
        ships = self.ships()
        if not ships:
            raise ValueError("config.toml has no [ships.\"...\"] profiles")
        return ships.get(self.get("ship", "active"), next(iter(ships.values())))

    def set_active_ship(self, name: str) -> None:
        self.set("ship", "active", name)
        self.save()

    def add_ship(self, ship: ShipProfile) -> None:
        if "ships" not in self.doc:
            self.doc["ships"] = tomlkit.table(is_super_table=True)
        t = tomlkit.table()
        if ship.uex_id:
            t["uex_id"] = ship.uex_id
        t["cargo_scu"] = ship.cargo_scu
        t["max_container_scu"] = ship.max_container_scu
        t["pad"] = ship.pad
        t["qt_speed_gm_s"] = ship.qt_speed_gm_s
        t["qt_spool_s"] = ship.qt_spool_s
        t["qt_fuel_scu_per_gm"] = ship.qt_fuel_scu_per_gm
        self.doc["ships"][ship.name] = t
        self.save()

    def remove_ship(self, name: str) -> None:
        """Drop a profile. The active ship can't be removed."""
        if name == self.active_ship().name:
            raise ValueError("switch to another ship before removing this one")
        if name in self.doc.get("ships", {}):
            del self.doc["ships"][name]
            self.save()

    def pad_overrides(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in self.section("pad_overrides").items()}

    # ocr region

    def ocr_region(self) -> tuple[int, tuple[int, int, int, int]] | None:
        """(monitor, (left, top, width, height)), or None when not calibrated."""
        region = [int(v) for v in self.get("ocr", "region", [0, 0, 0, 0])]
        if len(region) != 4 or region[2] <= 0 or region[3] <= 0:
            return None
        return int(self.get("ocr", "monitor", 1)), tuple(region)

    def set_ocr_region(self, monitor: int, region: tuple[int, int, int, int]) -> None:
        self.set("ocr", "monitor", int(monitor))
        arr = tomlkit.array()
        arr.extend(int(v) for v in region)
        self.set("ocr", "region", arr)
        self.save()


def read_token(path: Path = ENV_PATH) -> str:
    """UEX_TOKEN from .env, or "" when unset. Never print or log the result."""
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("UEX_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""
