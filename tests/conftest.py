import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sto.config import ShipProfile  # noqa: E402
from sto.models import build_dataset  # noqa: E402

FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="session")
def snapshot_raw() -> dict:
    return json.loads((FIXTURES / "snapshot.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def snapshot_ds(snapshot_raw):
    return build_dataset(snapshot_raw)


@pytest.fixture(scope="session")
def snapshot_now(snapshot_raw) -> float:
    return float(snapshot_raw["fetched_at"])


@pytest.fixture
def centaurus() -> ShipProfile:
    return ShipProfile(name="Constellation Mk V Centaurus", cargo_scu=240, max_container_scu=32, pad="L",
                       qt_speed_gm_s=0.2, qt_spool_s=10, qt_fuel_scu_per_gm=0.02, uex_id=292)
