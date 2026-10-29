"""python -m sto [--refresh | --test-ocr PNG | --calibrate | --add-ship NAME]"""
from __future__ import annotations

import argparse
import sys
import time

from .config import CACHE_DIR, Config, ShipProfile, read_token, vehicle_max_box
from .data import DataService
from .store import Store
from .uex import UexClient


def build_service(cfg: Config) -> tuple[DataService, Store]:
    store = Store(CACHE_DIR / "sto.sqlite")
    client = UexClient(token=read_token(), per_minute=int(cfg.get("data", "max_requests_per_min", 60)))
    svc = DataService(client, store,
                      price_refresh_s=float(cfg.get("data", "price_refresh_s", 300)),
                      fuel_refresh_s=float(cfg.get("data", "fuel_refresh_s", 3600)),
                      static_refresh_h=float(cfg.get("data", "static_refresh_h", 24)))
    return svc, store


def cmd_refresh(cfg: Config) -> int:
    from .optimizer import Settings, find_routes
    from .tui.fmt import age_text, compact

    svc, store = build_service(cfg)
    print("Fetching from UEX (bulk endpoints)...")
    errors = svc.refresh(force=True)
    for e in errors:
        print("  error:", e)
    print(f"  fetched: {', '.join(svc.fetched) or 'nothing'}")
    ds = svc.dataset(cfg.pad_overrides())
    if ds is None:
        print("No cached data to rank.")
        return 1
    saved = store.get_state("wallet") or {}
    wallet = int(saved.get("value") or cfg.get("wallet", "balance", 0))
    names = cfg.get("data", "systems", ["Stanton"])
    s = Settings(wallet=wallet, reserve=float(cfg.get("wallet", "reserve", 150_000)), ship=cfg.active_ship(),
                 now=time.time(), system_ids=frozenset(filter(None, (ds.system_id(n) for n in names))))
    routes = find_routes(ds, s)
    print(f"\n{len(ds.prices)} prices, {len(ds.terminals)} trade terminals. "
          f"Top routes for {wallet:,} aUEC, {s.ship.name}, {', '.join(names)}:")
    if wallet <= s.reserve:
        print("  (wallet is at or below the reserve: set [wallet] balance in config.toml or press w in the app)")
    for i, r in enumerate(routes, 1):
        print(f"{i:>2} {r.commodity.name[:16]:16} {r.buy_terminal.label[:18]:18} > {r.sell_terminal.label[:18]:18} "
              f"{r.qty:>3} SCU  profit {compact(r.profit):>6}  conf {r.confidence:.2f}  age {age_text(r.age_h):>4}  "
              f"{' '.join(r.flags)}")
    return 0 if not errors else 2


def cmd_test_ocr(cfg: Config, path: str, region: list[int] | None) -> int:
    from PIL import Image

    from .ocr.sanity import WalletSanity
    from .ocr.sync import WalletSync

    image = Image.open(path)
    box = None
    if region:
        box = tuple(region)
    elif (where := cfg.ocr_region()) is not None:
        x, y, w, h = where[1]
        if x + w <= image.width and y + h <= image.height:
            box = (x, y, w, h)
    sync = WalletSync(region=lambda: None, sanity=WalletSanity(), on_read=lambda r: None,
                      engine_kind=str(cfg.get("ocr", "engine", "auto")),
                      tesseract_cmd=str(cfg.get("ocr", "tesseract_cmd", "")))
    if not box:
        from .ocr.locate import find_balance

        print(f"Looking for the mobiGlas balance in {path} ...")
        found = find_balance(image, sync.engine())
        if found is None:
            print("No open mobiGlas app bar found. Pass --region X Y W H to read a specific box.")
            return 1
        box = found.region
    x, y, w, h = box
    image = image.convert("RGB").crop((x, y, x + w, y + h))
    print(f"OCR region {box} of {path}")
    read = sync.read_image(image, apply=False)
    print(f"engine: {read.engine}")
    print(f"raw:    {read.text!r}")
    print(f"parsed: {read.value:,}" if read.value is not None else f"parsed: none ({read.reason})")
    return 0 if read.value is not None else 1


def cmd_calibrate(cfg: Config, monitor: int, countdown: int, region: list[int] | None,
                  image: str | None) -> int:
    if region:
        cfg.set_ocr_region(monitor, tuple(region))
        print(f"Saved monitor {monitor}, region {tuple(region)}")
        return 0
    from .ocr.calibrate import calibrate_image, calibrate_screen
    from .ocr.engine import make_engine

    engine = make_engine(str(cfg.get("ocr", "engine", "auto")), str(cfg.get("ocr", "tesseract_cmd", "")))
    if image:
        try:
            cal = calibrate_image(image, engine, preferred=monitor)
        except (OSError, ValueError) as e:
            print(f"Can't use {image}: {e}")
            return 1
    else:
        for left in range(countdown, 0, -1):
            print(f"  switch to the game and press F1 to open the mobiGlas: looking in {left}s", flush=True)
            time.sleep(1)
        cal = calibrate_screen(engine, preferred=monitor)
    if cal is None:
        print("No open mobiGlas found. Open it with F1 and try again, or pass --image with a screenshot.")
        return 1
    cfg.set_ocr_region(cal.monitor, cal.region)
    print(f"Found {cal.value:,} on {cal.source}. Saved monitor {cal.monitor}, region {cal.region}")
    return 0


def cmd_add_ship(cfg: Config, name: str, max_box: int | None) -> int:
    svc, _ = build_service(cfg)
    if not svc.vehicles():
        svc._fetch("vehicles", "vehicles")
    matches = svc.vehicles(name)
    if not matches:
        print(f"No UEX vehicle matches {name!r}.")
        return 1
    if len(matches) > 1:
        print(f"{len(matches)} vehicles match {name!r}; be more specific:")
        for v in matches[:20]:
            print(f"  {v['name_full']}  ({v.get('scu', 0)} SCU, pad {v.get('pad_type') or '?'})")
        return 1
    v = matches[0]
    box = max_box or vehicle_max_box(v)
    if box is None:
        answer = input(f"UEX has no container sizes for {v['name_full']}. Biggest box it takes (SCU): ").strip()
        if not answer.isdigit():
            print("Need a whole number.")
            return 1
        box = int(answer)
    ship = ShipProfile.from_vehicle(v, box)
    cfg.add_ship(ship)
    print(f"Added {ship.name}: {ship.cargo_scu} SCU, box <={ship.max_container_scu}, pad {ship.pad or '?'}.")
    print("Tune qt_speed_gm_s / qt_spool_s / qt_fuel_scu_per_gm in config.toml. In the app, s switches ships.")
    return 0


def cmd_check_ocr() -> int:
    """For setup.ps1: 0 = Windows OCR works, 2 = path too long, 3 = no OCR language, 4 = other."""
    from .ocr.engine import OcrUnavailable, WindowsOcr

    try:
        WindowsOcr()
    except OcrUnavailable as e:
        print(e)
        return {"path_too_long": 2, "no_language": 3}.get(e.reason, 4)
    print("ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m sto", description="Star Citizen trade route optimizer (UEX data).")
    p.add_argument("--refresh", action="store_true", help="fetch everything from UEX and print the top routes")
    p.add_argument("--test-ocr", metavar="IMAGE", help="OCR a saved screenshot and print the parsed balance")
    p.add_argument("--calibrate", action="store_true",
                   help="find the balance on the open mobiGlas by OCR and save its region")
    p.add_argument("--image", metavar="FILE", help="with --calibrate: use a saved full-screen screenshot")
    p.add_argument("--monitor", type=int, default=None, help="monitor number for --calibrate (1 = first)")
    p.add_argument("--countdown", type=int, default=5, help="seconds to open the mobiGlas before --calibrate looks")
    p.add_argument("--region", type=int, nargs=4, metavar=("X", "Y", "W", "H"),
                   help="explicit region for --calibrate or --test-ocr")
    p.add_argument("--add-ship", metavar="NAME", help="add a ship profile from the UEX vehicles list")
    p.add_argument("--max-box", type=int, help="biggest container (SCU) for --add-ship when UEX has none")
    p.add_argument("--check-ocr", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.check_ocr:
        return cmd_check_ocr()
    cfg = Config()

    if args.refresh:
        return cmd_refresh(cfg)
    if args.test_ocr:
        return cmd_test_ocr(cfg, args.test_ocr, args.region)
    if args.calibrate:
        where = cfg.ocr_region()
        monitor = args.monitor or (where[0] if where else 1)
        return cmd_calibrate(cfg, monitor, args.countdown, args.region, args.image)
    if args.add_ship:
        return cmd_add_ship(cfg, args.add_ship, args.max_box)

    from .tui.app import TradeApp

    svc, store = build_service(cfg)
    TradeApp(cfg, svc, store).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
