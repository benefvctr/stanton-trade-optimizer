"""What a run earned, from balance readings in order: a fall (buying), then a
rise (selling). Readings can miss steps; two runs must never be added up."""
from sto.store import Store
from sto.wallet_log import Entry, last_run, session_change

MIN = 60


def log(*steps):
    """steps: (minute, balance) pairs."""
    return [Entry(m * MIN, v, "ocr") for m, v in steps]


def test_buy_fly_sell_earns_sale_minus_balance_before_buying():
    run = last_run(log((0, 2_000_000), (1, 1_500_000), (25, 2_400_000)))
    assert (run.change, run.in_progress, run.since) == (400_000, False, 0)


def test_while_cargo_is_aboard_it_shows_what_was_spent():
    run = last_run(log((0, 2_000_000), (1, 1_500_000)))
    assert (run.change, run.in_progress) == (-500_000, True)


def test_selling_in_batches_at_one_terminal_is_one_run():
    run = last_run(log((0, 2_000_000), (1, 1_500_000), (25, 1_900_000), (27, 2_400_000)))
    assert run.change == 400_000


def test_fuel_and_fines_on_the_way_stay_inside_the_run():
    # Measured from the balance before buying, so the 10,000 refuel is already paid out of the 2.4M.
    run = last_run(log((0, 2_000_000), (1, 1_500_000), (12, 1_490_000), (25, 2_400_000)))
    assert (run.change, run.since) == (400_000, 0)


def test_a_missed_buy_reading_never_adds_two_runs_together():
    steps = log((0, 2_000_000), (1, 1_500_000), (25, 2_400_000),     # run 1, read fully
                (60, 2_900_000))                                        # run 2: only the sale was read
    assert last_run(steps).change == 500_000


def test_a_loss_shows_as_negative():
    run = last_run(log((0, 2_000_000), (1, 1_500_000), (25, 1_900_000)))
    assert (run.change, run.in_progress) == (-100_000, False)


def test_one_reading_is_not_a_run():
    assert last_run(log((0, 2_000_000))) is None and session_change(log((0, 2_000_000))) is None
    assert session_change(log((0, 2_000_000), (1, 1_500_000), (25, 2_400_000))) == 400_000


def test_the_store_skips_a_reading_that_repeats_the_last(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    assert store.log_wallet(1.0, 100, "ocr") and not store.log_wallet(2.0, 100, "terminal")
    assert store.log_wallet(3.0, 90, "manual")
    assert store.wallet_log() == [Entry(1.0, 100, "ocr"), Entry(3.0, 90, "manual")]
    assert store.wallet_log(newer_than=2.0) == [Entry(3.0, 90, "manual")]
