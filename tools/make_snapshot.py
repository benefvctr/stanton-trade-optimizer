"""Build fixtures/snapshot.json, the test dataset, from the full raw responses.

Keeps real records for the three live systems (Stanton, Pyro, Nyx), trimmed to
the fields the app reads, so the committed fixture stays small. fetched_at is
the raw prices file's modification time, which tests use as a frozen "now".

Usage: python tools/make_snapshot.py   (after tools/explore_api.py)
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "fixtures" / "raw"
LIVE_SYSTEMS = {68, 64, 55}
TRADE_TYPES = {"commodity", "commodity_raw"}

TERMINAL_KEYS = [
    "id", "id_star_system", "id_planet", "id_orbit", "id_moon", "id_space_station", "id_outpost",
    "id_poi", "id_city", "name", "nickname", "code", "type", "is_available_live", "is_player_owned",
    "is_auto_load", "has_loading_dock", "has_docking_port", "has_freight_elevator", "star_system_name",
    "planet_name", "orbit_name", "moon_name", "space_station_name", "outpost_name", "city_name",
    "faction_name", "max_container_size",
]
PRICE_KEYS = [
    "id_commodity", "id_terminal", "price_buy", "price_buy_avg", "price_sell", "price_sell_avg",
    "scu_buy", "scu_buy_avg", "scu_sell_stock", "scu_sell_stock_avg", "scu_sell", "scu_sell_avg",
    "status_buy", "status_sell", "container_sizes", "date_modified", "commodity_name", "terminal_name",
]
COMMODITY_KEYS = ["id", "name", "code", "kind", "is_illegal", "is_buyable", "is_sellable"]
LOCATION_KEYS = ["id", "id_star_system", "name", "nickname", "pad_types", "has_docking_port", "has_loading_dock", "has_freight_elevator"]
ORBIT_KEYS = ["id_star_system_origin", "id_star_system_destination", "id_orbit_origin", "id_orbit_destination", "distance", "orbit_origin_name", "orbit_destination_name"]
VEHICLE_KEYS = ["id", "name", "name_full", "scu", "container_sizes", "pad_type", "is_spaceship", "is_concept",
                "is_ground_vehicle", "company_name"]
FUEL_KEYS = ["id_commodity", "id_terminal", "price_buy", "date_modified", "commodity_name", "terminal_name"]


def rows(name: str) -> list[dict]:
    return json.loads((RAW / f"{name}.json").read_text(encoding="utf-8"))["data"]


def pick(row: dict, keys: list[str]) -> dict:
    return {k: row.get(k) for k in keys}


def main() -> None:
    fuel = rows("fuel_prices_all")
    fuel_terminals = {f["id_terminal"] for f in fuel}
    terminals = [t for t in rows("terminals") if t["id_star_system"] in LIVE_SYSTEMS
                 and (t["type"] in TRADE_TYPES or t["id"] in fuel_terminals)]
    terminal_ids = {t["id"] for t in terminals}
    orbits = []
    for sid in sorted(LIVE_SYSTEMS):
        orbits += [pick(o, ORBIT_KEYS) for o in rows(f"orbits_distances__id_star_system-{sid}")]
    snapshot = {
        "fetched_at": int((RAW / "commodities_prices_all.json").stat().st_mtime),
        "star_systems": [pick(s, ["id", "name", "code", "is_available_live"]) for s in rows("star_systems") if s["id"] in LIVE_SYSTEMS],
        "terminals": [pick(t, TERMINAL_KEYS) for t in terminals],
        "commodities": [pick(c, COMMODITY_KEYS) for c in rows("commodities")],
        "commodities_prices_all": [pick(p, PRICE_KEYS) for p in rows("commodities_prices_all") if p["id_terminal"] in terminal_ids],
        "commodities_status": json.loads((RAW / "commodities_status.json").read_text(encoding="utf-8"))["data"],
        "orbits_distances": orbits,
        "vehicles": [pick(v, VEHICLE_KEYS) for v in rows("vehicles") if (v.get("scu") or 0) > 0],
        "fuel_prices_all": [pick(f, FUEL_KEYS) for f in fuel if f["id_terminal"] in terminal_ids],
    }
    for loc in ["space_stations", "outposts", "cities", "poi"]:
        snapshot[loc] = [pick(r, LOCATION_KEYS) for r in rows(loc) if r["id_star_system"] in LIVE_SYSTEMS]
    out = ROOT / "fixtures" / "snapshot.json"
    out.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {out.name}: {out.stat().st_size // 1024} KB, "
          + ", ".join(f"{k}={len(v)}" for k, v in snapshot.items() if isinstance(v, list)))


if __name__ == "__main__":
    main()
