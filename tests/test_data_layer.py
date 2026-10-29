"""UEX client (backoff, rate limit, status handling), SQLite cache, and the
refresh service's stale-cache behaviour. No network: a fake transport replays
real response bodies from fixtures/."""
import json

import pytest

from sto.data import FUEL, PRICES, DataService, orbit_key
from sto.models import build_dataset
from sto.store import Store
from sto.uex import BACKOFF_S, RateLimiter, UexClient, UexError

from conftest import FIXTURES


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Script:
    """Transport that returns queued (status, body) pairs and records calls."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(data):
    return 200, json.dumps({"status": "ok", "http_code": 200, "data": data})


def client(transport, clock=None, token=""):
    clock = clock or FakeClock()
    return UexClient(token=token, transport=transport, sleep=clock.sleep,
                     limiter=RateLimiter(1000, clock=clock, sleep=clock.sleep)), clock


# --- client --------------------------------------------------------------------

def test_returns_data_and_sends_token_only_when_set():
    t = Script(ok([1, 2]), ok([3]))
    c, _ = client(t, token="secret")
    assert c.get("commodities") == [1, 2]
    assert t.calls[0][1]["Authorization"] == "Bearer secret"
    c2, _ = client(t)
    c2.get("commodities")
    assert "Authorization" not in t.calls[1][1]


def test_backs_off_on_429_and_5xx_then_succeeds():
    t = Script((429, "{}"), (503, "<html>down</html>"), ok({"x": 1}))
    c, clock = client(t)
    start = clock.t
    assert c.get("terminals") == {"x": 1}
    assert clock.t - start == pytest.approx(BACKOFF_S[0] + BACKOFF_S[1])


def test_requests_limit_reached_status_is_retried_even_with_http_200():
    t = Script((200, json.dumps({"status": "requests_limit_reached", "data": None})), ok([7]))
    c, _ = client(t)
    assert c.get("commodities_prices_all") == [7]


def test_gives_up_after_all_backoff_steps():
    t = Script(*[(500, "{}")] * (len(BACKOFF_S) + 1))
    c, clock = client(t)
    start = clock.t
    with pytest.raises(UexError):
        c.get("terminals")
    assert len(t.calls) == len(BACKOFF_S) + 1
    assert clock.t - start == pytest.approx(sum(BACKOFF_S))


def test_network_errors_are_retried():
    t = Script(OSError("no route"), ok([1]))
    c, _ = client(t)
    assert c.get("terminals") == [1]


def test_a_response_cut_off_mid_body_is_retried():
    # Seen live: UEX dropped the connection 373 KB into commodities_prices_all.
    import http.client
    t = Script(http.client.IncompleteRead(b'{"status":"ok","data":[{"id":2'), ok([1]))
    c, _ = client(t)
    assert c.get("commodities_prices_all") == [1]


def test_truncated_or_garbled_bodies_are_retried_then_reported():
    import http.client
    t = Script(http.client.IncompleteRead(b"x"), http.client.RemoteDisconnected("closed"),
               (200, '{"status":"ok","data":[{"id"'), (200, b"\xff\xfe".decode("latin-1")),
               *[http.client.IncompleteRead(b"")] * 2)
    c, _ = client(t)
    with pytest.raises(UexError, match="commodities_prices_all"):
        c.get("commodities_prices_all")
    assert len(t.calls) == len(BACKOFF_S) + 1


def test_error_status_is_raised_without_retry_using_a_real_error_body():
    body = (FIXTURES / "errors" / "terminals_distances.json").read_text(encoding="utf-8")
    t = Script((400, body))
    c, _ = client(t)
    with pytest.raises(UexError) as e:
        c.get("terminals_distances")
    assert e.value.status == "missing_id_terminal_origin"
    assert len(t.calls) == 1


def test_rate_limiter_caps_calls_per_minute():
    clock = FakeClock()
    limiter = RateLimiter(60, clock=clock, sleep=clock.sleep)
    start = clock.t
    for _ in range(120):
        limiter.acquire()
    # 60 burst immediately, the next 60 at one per second.
    assert clock.t - start == pytest.approx(60, abs=1.5)


# --- store ---------------------------------------------------------------------

def test_store_keeps_last_good_data_after_a_failure(tmp_path):
    s = Store(tmp_path / "c.sqlite")
    s.save("prices", [1, 2], at=100)
    s.mark_failure("prices", "boom", at=200)
    assert s.load("prices") == ([1, 2], 100)
    assert s.meta("prices") == {"fetched_at": 100, "last_attempt": 200, "last_error": "boom"}
    s.save("prices", [3], at=300)
    assert s.meta("prices")["last_error"] is None


def test_store_distances_and_state(tmp_path):
    s = Store(tmp_path / "c.sqlite")
    s.save_terminal_distance(9, 3, 4.5, at=100)
    assert s.terminal_distances() == {(3, 9): 4.5}
    assert s.terminal_distances(newer_than=101) == {}
    s.set_state("wallet", {"value": 5, "source": "ocr"})
    assert s.get_state("wallet") == {"value": 5, "source": "ocr"}
    assert s.get_state("missing", "d") == "d"


# --- service -------------------------------------------------------------------

def snapshot_transport(snapshot_raw):
    """Answers any endpoint from the real snapshot; fails prices when told to."""
    state = {"fail_prices": False, "calls": []}

    def transport(url, headers, timeout):
        resource = url.split("/2.0/")[1]
        state["calls"].append(resource)
        name = resource.split("?")[0]
        if name == PRICES and state["fail_prices"]:
            return 503, "{}"
        if name == "orbits_distances":
            sid = int(resource.split("=")[1])
            return ok([o for o in snapshot_raw["orbits_distances"] if o["id_star_system_origin"] == sid])
        if name == "terminals_distances":
            return ok({"distance": "12"})
        return ok(snapshot_raw.get(name, []))

    return transport, state


def service(snapshot_raw, tmp_path):
    transport, state = snapshot_transport(snapshot_raw)
    clock = FakeClock(snapshot_raw["fetched_at"])
    c = UexClient(transport=transport, sleep=clock.sleep, limiter=RateLimiter(1000, clock=clock, sleep=clock.sleep))
    svc = DataService(c, Store(tmp_path / "c.sqlite"), price_refresh_s=300, clock=clock, sleep=clock.sleep)
    return svc, state, clock


def test_first_refresh_fetches_bulk_endpoints_not_per_commodity(snapshot_raw, tmp_path):
    svc, state, _ = service(snapshot_raw, tmp_path)
    assert svc.refresh() == []
    assert PRICES in state["calls"] and FUEL in state["calls"]
    assert not any(c.startswith("commodities_prices?") for c in state["calls"])
    assert len(state["calls"]) == 9 + 3 + 2       # static, one orbit call per live system, prices, fuel
    ds = svc.dataset()
    assert len(ds.prices) == len(snapshot_raw["commodities_prices_all"])


def test_only_prices_are_refetched_after_five_minutes(snapshot_raw, tmp_path):
    svc, state, clock = service(snapshot_raw, tmp_path)
    svc.refresh()
    state["calls"].clear()
    clock.t += 301
    svc.refresh()
    assert state["calls"] == [PRICES]
    clock.t += 3600
    state["calls"].clear()
    svc.refresh()
    assert state["calls"] == [PRICES, FUEL]


def test_api_down_keeps_the_cache_and_reports_stale_since(snapshot_raw, tmp_path):
    svc, state, clock = service(snapshot_raw, tmp_path)
    svc.refresh()
    good_at = clock.t
    assert svc.freshness().stale_since is None
    clock.t += 301
    state["fail_prices"] = True
    errors = svc.refresh()
    assert errors and "server error" in errors[0]
    f = svc.freshness()
    assert f.stale_since == pytest.approx(good_at)
    assert f.last_error
    assert svc.dataset() is not None and svc.dataset().prices        # still serving the cache
    state["fail_prices"] = False
    clock.t += 61
    assert svc.refresh() == []
    assert svc.freshness().stale_since is None


def test_lookup_distances_is_paced_cached_and_feeds_the_dataset(snapshot_raw, tmp_path):
    svc, state, clock = service(snapshot_raw, tmp_path)
    svc.refresh()
    ds = svc.dataset()
    stanton = [t for t in ds.terminals.values() if t.system == "Stanton"]
    a, b, c = stanton[0].id, stanton[1].id, stanton[2].id
    start = clock.t
    assert svc.lookup_distances([(a, b), (b, a), (a, c)], pace_s=3.0) == 2
    assert clock.t - start >= 3.0
    assert svc.lookup_distances([(a, b)]) == 0                        # cached
    assert svc.dataset().terminal_gm[tuple(sorted((a, b)))] == 12.0


def test_stanton_filter_uses_real_system_ids(snapshot_raw):
    ds = build_dataset(snapshot_raw)
    assert ds.system_id("Stanton") == 68
    stanton = {t.id for t in ds.terminals.values() if t.system_id == 68}
    assert len(stanton) == 128                                        # 117 commodity + 11 raw
    assert {t.system for t in ds.terminals.values()} == {"Stanton", "Pyro", "Nyx"}


def test_orbit_keys_follow_the_real_query():
    assert orbit_key(68) == "orbits_distances?id_star_system=68"
