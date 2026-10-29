"""The read pipeline with a fake capture and engine, plus Windows OCR on the
real mobiGlas screenshot."""
import logging
import threading

import pytest
from PIL import Image

from sto.ocr.parse import parse_balance
from sto.ocr.sanity import WalletSanity
from sto.ocr.sync import WalletSync

from conftest import FIXTURES

# Where the balance sits in fixtures/ocr/mobiglas-home.webp (2000x809 stream frame).
BALANCE_BOX = (545, 745, 120, 40)


class FakeEngine:
    name = "fake"

    def __init__(self, *texts):
        self.texts = list(texts)

    def read(self, image):
        return self.texts.pop(0) if self.texts else ""


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def make_sync(engine, region=(1, (10, 10, 120, 40)), last=None):
    log = logging.getLogger(f"test.ocr.{id(engine)}")
    handler = ListHandler()
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    reads = []
    sync = WalletSync(region=lambda: region, sanity=WalletSanity(60, last), on_read=reads.append,
                      grab=lambda monitor, box: Image.new("RGB", (box[2], box[3])), engine=engine, log=log,
                      sleep=lambda s: None)
    return sync, reads, handler.lines


def test_accepted_read_reaches_the_callback_and_the_log():
    sync, reads, log = make_sync(FakeEngine("¤ 2,257,549"))
    r = sync.read_screen()
    assert (r.value, r.accepted) == (2_257_549, True)
    assert reads == [r]
    assert log[-1].startswith("ACCEPT value=2257549")
    assert "raw='¤ 2,257,549'" in log[-1]


def test_unreadable_variant_falls_through_to_the_next():
    sync, _, _ = make_sync(FakeEngine("HOME", "2,257.549"))
    assert sync.read_screen().value == 2_257_549


def test_closing_press_is_logged_as_rejected():
    sync, reads, log = make_sync(FakeEngine("", "", ""), last=2_000_000)
    r = sync.read_screen()
    assert not r.accepted and r.reason == "not a number"
    assert log[-1].startswith("REJECT value=None")
    assert reads == [r]


def test_big_jump_needs_two_reads():
    sync, _, log = make_sync(FakeEngine("9,000,000", "9,000,000"), last=2_000_000)
    assert not sync.read_screen().accepted
    assert sync.read_screen().accepted
    assert "waiting for a second matching read" in log[0]


def test_uncalibrated_region_is_rejected_without_capturing():
    sync, reads, _ = make_sync(FakeEngine("1"), region=None)
    r = sync.read_screen()
    assert not r.accepted and "not calibrated" in r.reason


def test_held_key_triggers_one_read_at_a_time():
    release = threading.Event()
    sync, reads, _ = make_sync(FakeEngine("1,000", "2,000"))
    sync.sleep = lambda s: release.wait(5)
    sync._pressed()
    sync._pressed()                     # still waiting on the first: ignored
    release.set()
    for _ in range(100):
        if reads:
            break
        threading.Event().wait(0.02)
    threading.Event().wait(0.1)
    assert [r.value for r in reads] == [1_000]


def test_a_missed_read_searches_nearby_and_recentres_the_box():
    from sto.ocr.locate import Found

    sync, reads, log = make_sync(FakeEngine(), region=(1, (500, 700, 120, 40)))
    moved, searched = [], []

    def locate(image, engine, near):
        searched.append((image.size, near))
        return Found((130, 85, 120, 40), 2_275_177, "2.275177")

    sync.on_region_moved = lambda monitor, box: moved.append((monitor, box))
    sync.monitor_size = lambda monitor: (3440, 1440)
    sync.locate = locate
    r = sync.read_screen()
    assert (r.value, r.accepted) == (2_275_177, True)
    assert searched == [((360, 200), (120, 80, 120, 40))]   # band = 3 boxes wide, 5 high around the old one
    assert moved == [(1, (510, 705, 120, 40))]
    assert "moved to" in log[-1]


def test_a_closed_mobiglas_stays_rejected_after_the_nearby_search():
    sync, reads, _ = make_sync(FakeEngine(), region=(1, (500, 700, 120, 40)), last=2_000_000)
    sync.on_region_moved = lambda monitor, box: pytest.fail("nothing should move")
    sync.monitor_size = lambda monitor: (3440, 1440)
    sync.locate = lambda image, engine, near: None
    r = sync.read_screen()
    assert not r.accepted and r.reason == "not a number"


def test_test_mode_skips_sanity_and_callback():
    sync, reads, _ = make_sync(FakeEngine("9,999,999"), last=10)
    r = sync.read_image(Image.new("RGB", (50, 20)), apply=False)
    assert r.value == 9_999_999 and r.reason == "test"
    assert reads == [] and sync.sanity.last == 10


def windows_ocr():
    from sto.ocr.engine import OcrUnavailable, WindowsOcr
    try:
        return WindowsOcr()
    except OcrUnavailable as e:
        pytest.skip(f"Windows OCR unavailable: {e}")


def test_windows_ocr_reads_the_real_mobiglas_balance():
    engine = windows_ocr()
    image = Image.open(FIXTURES / "ocr" / "mobiglas-home.webp")
    x, y, w, h = BALANCE_BOX
    sync, _, _ = make_sync(engine)
    r = sync.read_image(image.crop((x, y, x + w, y + h)), apply=False)
    assert r.value == 2_257_549, r.text
    assert parse_balance(r.text.split(" | ")[0]) == 2_257_549


def test_after_a_good_read_f1_rests_for_the_cooldown():
    sync, reads, _ = make_sync(FakeEngine("1,000,000", "1,000,500", "1,001,000"))
    clock = {"t": 1000.0}
    sync.clock = lambda: clock["t"]
    assert sync.cooldown_s == 180 and sync.delay_s == 2.0          # the defaults
    sync._pressed()
    wait_for(lambda: len(reads) == 1)
    assert sync.next_read_at() == 1180
    clock["t"] = 1100
    sync._pressed()                                                # inside the 3 minutes: no read
    threading.Event().wait(0.1)
    assert len(reads) == 1
    clock["t"] = 1180
    assert sync.next_read_at() is None
    sync._pressed()
    wait_for(lambda: len(reads) == 2)
    assert reads[1].value == 1_000_500


def test_a_missed_read_does_not_start_the_cooldown():
    from sto.ocr.capture import ocr_variants

    tries = len(list(ocr_variants(Image.new("RGB", (120, 40)))))       # every text size comes back empty
    sync, reads, _ = make_sync(FakeEngine(*[""] * tries, "1,000,000"), last=1_000_000)
    sync._pressed()                                                # mobiGlas not open yet: nothing read
    wait_for(lambda: len(reads) == 1)
    assert not reads[0].accepted and sync.next_read_at() is None
    sync._pressed()
    wait_for(lambda: len(reads) == 2)
    assert reads[1].accepted


def wait_for(condition, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        threading.Event().wait(0.02)
    raise AssertionError("timed out")
