"""Balance parsing from raw OCR text, and the accept/reject rules for reads."""
import pytest

from sto.ocr.parse import parse_balance
from sto.ocr.sanity import WalletSanity


@pytest.mark.parametrize("text, expected", [
    ("2,145,880 aUEC", 2_145_880),
    ("2.145.880", 2_145_880),
    ("Z,145,88O", 2_145_880),
    ("¤ 2,257,549", 2_257_549),            # how the mobiGlas app bar shows it
    ("¤2,257,549", 2_257_549),
    ("D 2,257,549", 2_257_549),            # the ¤ glyph misread as a letter
    ("B2,257,549", 2_257_549),             # ...or glued to the number
    ("2,257,549\nBENEFVCTR", 2_257_549),   # the player name sits under the balance
    ("2 257 549", 2_257_549),
    ("2,257.549\nBEEFVCTR", 2_257_549),    # real Windows OCR output on fixtures/ocr/mobiglas-home.webp
    ("2257.549", 2_257_549),               # same image, tighter crop: first comma dropped
    ("2.275177", 2_275_177),               # real OCR of mobiglas-home-2.webp: second separator dropped
    ("12,275177", 12_275_177),
    ("2, 257, 549", 2_257_549),
    ("l,234,567", 1_234_567),
    ("S,OOO aUEC", 5_000),
    ("2,145,880aUEC", 2_145_880),
    ("150000", 150_000),
    ("0", 0),
    ("  ¤ 980 ", 980),
])
def test_parses_balances(text, expected):
    assert parse_balance(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "BENEFVCTR", "aUEC", "HOME HEALTH COMMS", "2,25,549", "2,2575,49", "2,275,17", "¤ ,,, ",
])
def test_rejects_non_balances(text):
    assert parse_balance(text) is None


def test_first_read_is_accepted_without_a_baseline():
    s = WalletSanity(big_change_pct=60)
    assert s.offer(2_257_549) == (True, "accepted")


def test_small_change_is_accepted_at_once():
    s = WalletSanity(big_change_pct=60, last=2_000_000)
    assert s.offer(2_900_000)[0]                 # +45%
    assert s.last == 2_900_000


def test_big_change_needs_two_matching_reads():
    s = WalletSanity(big_change_pct=60, last=2_000_000)
    accepted, reason = s.offer(9_000_000)
    assert not accepted and "second" in reason
    assert s.last == 2_000_000
    assert s.offer(9_000_000) == (True, "accepted (confirmed)")
    assert s.last == 9_000_000


def test_a_different_big_read_replaces_the_pending_one():
    s = WalletSanity(big_change_pct=60, last=2_000_000)
    s.offer(9_000_000)
    assert not s.offer(500_000)[0]               # -75%, a new candidate
    assert not s.offer(9_000_000)[0]             # the first candidate was replaced
    assert s.offer(9_000_000)[0]


def test_failed_reads_between_matching_reads_do_not_reset_the_pending_value():
    # Pressing F1 to close the mobiGlas always produces a failed read.
    s = WalletSanity(big_change_pct=60, last=2_000_000)
    s.offer(9_000_000)
    assert s.offer(None) == (False, "not a number")
    assert s.offer(9_000_000)[0]


def test_non_numeric_reads_are_rejected():
    s = WalletSanity()
    assert s.offer(None) == (False, "not a number")
    assert s.last is None


def test_manual_entry_becomes_the_new_baseline():
    s = WalletSanity(big_change_pct=60, last=2_000_000)
    s.offer(9_000_000)
    s.reset(9_100_000)
    assert s.offer(9_200_000)[0]
