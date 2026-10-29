"""Finding the balance on a full mobiGlas frame without any dragging, using
Windows OCR on real screenshots."""
import pytest
from PIL import Image

from sto.ocr.locate import find_balance, scale_region

from conftest import FIXTURES


@pytest.fixture(scope="module")
def engine():
    from sto.ocr.engine import OcrUnavailable, WindowsOcr
    try:
        return WindowsOcr()
    except OcrUnavailable as e:
        pytest.skip(f"Windows OCR unavailable: {e}")


def inside(region, point):
    x, y, w, h = region
    return x <= point[0] <= x + w and y <= point[1] <= y + h


@pytest.mark.parametrize("name, value, digit_point", [
    ("mobiglas-home.webp", 2_257_549, (600, 762)),
    ("mobiglas-home-2.webp", 2_275_177, (600, 765)),
])
def test_finds_the_balance_from_the_app_bar(engine, name, value, digit_point):
    image = Image.open(FIXTURES / "ocr" / name).convert("RGB")
    found = find_balance(image, engine)
    assert found is not None
    assert found.value == value
    assert inside(found.region, digit_point)
    x, y, w, h = found.region
    assert w < 250 and h < 60                  # a tight box, not half the screen
    assert x > 520                             # clear of the spinner icon at ~495-517


def test_reports_nothing_when_the_mobiglas_is_closed(engine):
    image = Image.open(FIXTURES / "ocr" / "no-mobiglas.webp").convert("RGB")
    assert find_balance(image, engine) is None


def test_relocates_near_an_old_region_after_drift(engine):
    image = Image.open(FIXTURES / "ocr" / "mobiglas-home-2.webp").convert("RGB")
    stale = (520, 735, 120, 40)                # the box from the other screenshot, shifted
    found = find_balance(image, engine, near=stale)
    assert found is not None and found.value == 2_275_177


def test_scales_a_region_from_a_downscaled_screenshot():
    assert scale_region((560, 750, 100, 30), (2000, 837), (3440, 1440)) == (963, 1290, 172, 52)
