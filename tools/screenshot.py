"""Render the app headlessly from fixtures/snapshot.json and save an SVG
screenshot to docs/screenshot.svg (no network, no real config or cache)."""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from sto.config import Config  # noqa: E402
from sto.data import DataService  # noqa: E402
from sto.store import Store  # noqa: E402
from sto.tui.app import TradeApp  # noqa: E402
from sto.uex import RateLimiter, UexClient  # noqa: E402
from test_data_layer import FakeClock, snapshot_transport  # noqa: E402
import json  # noqa: E402


async def main(out: Path, keys: list[str], size=(140, 46)) -> None:
    raw = json.loads((ROOT / "fixtures" / "snapshot.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(ROOT / "config.default.toml", tmp / "config.toml")
    cfg = Config(tmp / "config.toml")
    cfg.set("wallet", "balance", 1_500_000)
    transport, _ = snapshot_transport(raw)
    clock = FakeClock(raw["fetched_at"])
    client = UexClient(transport=transport, sleep=clock.sleep, limiter=RateLimiter(1000, clock=clock, sleep=clock.sleep))
    store = Store(tmp / "c.sqlite")
    app = TradeApp(cfg, DataService(client, store, clock=clock, sleep=clock.sleep), store, clock=clock, start_ocr=False)
    async with app.run_test(size=size) as pilot:
        for _ in range(5):
            await pilot.pause()
            try:
                await app.workers.wait_for_complete()
            except Exception:
                pass
        app.ocr_status = "[#7ddc6a]●[/] F1 ready"
        app.update_status()
        await pilot.press(*(keys or ["down", "down"]))
        for _ in range(3):
            await pilot.pause()
        app.save_screenshot(filename=out.name, path=str(out.parent))
    print(f"saved {out}")


if __name__ == "__main__":
    # python tools/screenshot.py [name.svg] [keys ...], e.g. ships.svg s a c a t
    asyncio.run(main(ROOT / "docs" / (sys.argv[1] if len(sys.argv) > 1 else "screenshot.svg"), sys.argv[2:]))
