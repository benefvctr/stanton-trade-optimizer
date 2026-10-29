"""Calibration without dragging: on a saved screenshot or on each monitor."""
import pytest
from PIL import Image

from sto.ocr.calibrate import calibrate_image, calibrate_screen, newest_screenshot, pick_monitor

from conftest import FIXTURES

ULTRAWIDE = [{"left": 0, "top": 0, "width": 5360, "height": 1440},      # mss: 0 = all monitors
             {"left": 0, "top": 0, "width": 3440, "height": 1440},
             {"left": 3440, "top": 0, "width": 1920, "height": 1080}]


@pytest.fixture(scope="module")
def engine():
    from sto.ocr.engine import OcrUnavailable, WindowsOcr
    try:
        return WindowsOcr()
    except OcrUnavailable as e:
        pytest.skip(f"Windows OCR unavailable: {e}")


def test_screenshot_maps_onto_the_monitor_it_came_from(engine):
    # The fixture is a 3440x1440 frame resized to 2000x837.
    cal = calibrate_image(FIXTURES / "ocr" / "mobiglas-home-2.webp", engine, monitors=ULTRAWIDE)
    assert cal.monitor == 1 and cal.value == 2_275_177
    x, y, w, h = cal.region
    digit = (600 * 3440 / 2000, 765 * 1440 / 837)          # a pixel inside the digits, scaled up
    assert x <= digit[0] <= x + w and y <= digit[1] <= y + h


def test_screen_search_tries_every_monitor(engine):
    frames = {1: Image.open(FIXTURES / "ocr" / "no-mobiglas.webp"),
              2: Image.open(FIXTURES / "ocr" / "mobiglas-home.webp")}
    cal = calibrate_screen(engine, preferred=1, monitors=ULTRAWIDE, grab=lambda i: frames[i])
    assert (cal.monitor, cal.value) == (2, 2_257_549)


def test_screen_search_reports_a_closed_mobiglas(engine):
    frame = Image.open(FIXTURES / "ocr" / "no-mobiglas.webp")
    assert calibrate_screen(engine, monitors=ULTRAWIDE, grab=lambda i: frame) is None


def test_screenshot_of_the_wrong_shape_is_refused():
    with pytest.raises(ValueError, match="shape"):
        pick_monitor((2000, 837), [ULTRAWIDE[0], ULTRAWIDE[2]])
    assert pick_monitor((1920, 1080), ULTRAWIDE) == 2
    assert pick_monitor((3440, 1440), ULTRAWIDE, preferred=2) == 1


def test_newest_screenshot(tmp_path):
    assert newest_screenshot([tmp_path / "missing"]) is None
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    assert newest_screenshot([tmp_path]).name == "a.jpg"
