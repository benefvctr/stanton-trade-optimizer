"""UEX API 2.0 client: optional Bearer token, client-side rate cap, backoff."""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

BASE_URL = "https://api.uexcorp.uk/2.0/"
USER_AGENT = "stanton-trade-optimizer/0.1"
BACKOFF_S = (2, 4, 8, 16, 32)

# (url, headers, timeout) -> (http status, body text). Raises OSError on network failure.
Transport = Callable[[str, dict, float], tuple[int, str]]


class UexError(Exception):
    def __init__(self, message: str, status: str | None = None, http_code: int | None = None):
        super().__init__(message)
        self.status = status
        self.http_code = http_code


def urllib_transport(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class RateLimiter:
    """Token bucket: at most `per_minute` calls in any minute, bursts up to that."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.capacity = float(max(1, per_minute))
        self.rate = self.capacity / 60.0
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.last = clock()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            while True:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                self.sleep((1 - self.tokens) / self.rate)


class UexClient:
    def __init__(self, token: str = "", per_minute: int = 60, transport: Transport = urllib_transport,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0,
                 limiter: RateLimiter | None = None):
        self._token = token
        self.transport = transport
        self.sleep = sleep
        self.timeout = timeout
        self.limiter = limiter or RateLimiter(per_minute, sleep=sleep)

    def _headers(self) -> dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def get(self, resource: str, params: dict | None = None):
        """Return the response's "data". Retries 429, 5xx, network errors and
        requests_limit_reached with backoff; raises UexError when they persist
        or on any other error status."""
        url = BASE_URL + resource
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last: UexError | None = None
        for attempt in range(len(BACKOFF_S) + 1):
            if attempt:
                self.sleep(BACKOFF_S[attempt - 1])
            self.limiter.acquire()
            try:
                code, body = self.transport(url, self._headers(), self.timeout)
            except (OSError, http.client.HTTPException) as e:
                # HTTPException covers a body cut off mid-transfer (IncompleteRead),
                # which UEX did once on the 1 MB prices response.
                last = UexError(f"{resource}: network error: {e!r}"[:300])
                continue
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            status = payload.get("status") if isinstance(payload, dict) else None
            if status == "requests_limit_reached" or code == 429:
                last = UexError(f"{resource}: rate limit reached", "requests_limit_reached", code)
                continue
            if code >= 500:
                last = UexError(f"{resource}: server error HTTP {code}", status, code)
                continue
            if not isinstance(payload, dict):
                last = UexError(f"{resource}: response was not complete JSON (HTTP {code})", None, code)
                continue
            if status != "ok" or code >= 400:
                message = payload.get("message") or status or f"HTTP {code}"
                raise UexError(f"{resource}: {message}", status, code)
            return payload.get("data")
        assert last is not None
        raise last
