"""HTTP layer: live, record and replay modes, per-domain rate limiting, retries and a run budget.

Every network call in the pipeline goes through `Fetcher.get`, so politeness rules and the
time budget are enforced in one place instead of being re-implemented by each source.
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

# A browser User-Agent. Airbnb only includes the listing's rating data in pages it serves to browsers (checked
# live on 2026-11-19: an honest User-Agent gets a page without it). That makes the Airbnb collector scraping,
# which is why production swaps it for Airbnb's partner API. Booking.com requests send an honest one instead.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


class BudgetExceeded(Exception):
    """The run-level time budget ran out."""


class FixtureMissing(Exception):
    """Replay mode asked for a response that was never recorded."""


@dataclass
class Response:
    status: int
    text: str
    url: str

    def json(self):
        return json.loads(self.text)


class Budget:
    """Wall-clock budget for a whole run, so a scheduled run can never overlap the next one."""

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise BudgetExceeded("run budget exhausted")


class RateLimiter:
    """Minimum spacing between requests to the same domain, plus jitter. Thread-safe."""

    def __init__(self, min_interval: dict[str, float] | None = None, default: float = 1.0):
        self.min_interval = min_interval or {}
        self.default = default
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, domain: str) -> None:
        interval = self.min_interval.get(domain, self.default)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(domain, now))
            self._next[domain] = slot + interval + random.uniform(0, interval * 0.3)
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def _domain(url: str) -> str:
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class Fetcher:
    """`mode` is "live" (network), "record" (network + save fixtures) or "replay" (fixtures only)."""

    def __init__(self, mode: str = "replay", fixtures_dir: Path | str = "fixtures",
                 budget: Budget | None = None, limiter: RateLimiter | None = None,
                 timeout: float = 15.0, retries: int = 2):
        if mode not in {"live", "record", "replay"}:
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.fixtures_dir = Path(fixtures_dir)
        self.budget = budget or Budget(10_000)
        self.limiter = limiter or RateLimiter({"airbnb.com": 0.6, "muscache.com": 0.2, "booking.com": 2.0})
        self.timeout = timeout
        self.retries = retries
        self._local = threading.local()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

    # -- fixtures -----------------------------------------------------------------------------
    def _fixture_path(self, key: str) -> Path:
        return self.fixtures_dir / f"{key}.json"

    def _replay(self, key: str) -> Response:
        path = self._fixture_path(key)
        if not path.exists():
            raise FixtureMissing(key)
        data = json.loads(path.read_text())
        return Response(data["status"], data["body"], data.get("url", ""))

    def _record(self, key: str, resp: Response) -> None:
        path = self._fixture_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": resp.status, "url": resp.url, "body": resp.text}))

    # -- public -------------------------------------------------------------------------------
    def thread_requests(self) -> int:
        """Requests made by the calling thread (each job runs on one thread)."""
        return getattr(self._local, "count", 0)

    def get(self, url: str, *, key: str, headers: dict | None = None) -> Response:
        """Fetch `url`. `key` names the fixture used in replay/record mode."""
        self.budget.check()
        self._local.count = getattr(self._local, "count", 0) + 1
        if self.mode == "replay":
            return self._replay(key)

        last_error = "request failed"
        for attempt in range(self.retries + 1):
            self.budget.check()
            self.limiter.wait(_domain(url))
            try:
                r = self.session.get(url, headers=headers, timeout=min(self.timeout, max(1.0, self.budget.remaining())))
            except requests.RequestException as exc:
                last_error = type(exc).__name__
            else:
                if r.status_code in (429, 503):
                    last_error = f"HTTP {r.status_code}"
                else:
                    resp = Response(r.status_code, r.text, r.url)
                    if self.mode == "record":
                        self._record(key, resp)
                    return resp
            if attempt < self.retries:
                time.sleep(min(2 ** attempt + random.random(), max(0.0, self.budget.remaining())))
        raise RuntimeError(last_error)
