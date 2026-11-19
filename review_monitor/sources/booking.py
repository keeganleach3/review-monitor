"""Booking.com: detect bot protection, report it, and stop.

Booking serves an AWS WAF JavaScript challenge (HTTP 202) to scripted traffic, on listing pages and
the review list alike. That is the site saying no, so this source does not try to get around it: the
job is marked `blocked_by_bot_protection` and the run reports it. Requests identify themselves honestly
(no browser User-Agent), and after the first challenge in a run the remaining homes are marked blocked
without being requested. For properties a customer manages, the sanctioned route is Booking's
Connectivity (partner) API. If a page does load normally, the public JSON-LD `aggregateRating` is read.
"""
from __future__ import annotations

import json
import re
import time

from ..http import BudgetExceeded, Fetcher
from ..models import BOOKING, JobResult, Metric, Status

WAF_MARKERS = ("awswafcookie", "aws-waf-token", "challenge.js", "gokuprops")  # AWS WAF's own markers
HONEST_UA = {"User-Agent": "review-monitor/1.0 (guest-review collector; stops at bot challenges)"}


def is_bot_challenge(status: int, body: str) -> bool:
    low = body[:20_000].lower()
    return status == 202 or any(m in low for m in WAF_MARKERS)


def _count(v) -> int | None:
    return int(float(v)) if v not in (None, "") else None  # absent stays unknown, never 0


def parse_json_ld(html: str) -> dict:
    """ratingValue / reviewCount from the page's JSON-LD structured data."""
    for block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            agg = isinstance(item, dict) and item.get("aggregateRating")
            if agg:
                return {"overall": float(agg.get("ratingValue")),
                        "review_count": _count(agg.get("reviewCount") or agg.get("ratingCount"))}
    return {}


class BookingSource:
    platform = BOOKING

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher
        self.blocked_at: int | None = None  # first home that got the challenge this run

    def collect(self, home, cutoff, previous_review_count) -> JobResult:
        res = JobResult(home=home.id, platform=BOOKING)
        if self.blocked_at is not None:  # the site already said no this run: don't ask again
            res.status = Status.BLOCKED
            res.error = f"not requested: bot challenge already served this run (home {self.blocked_at})"
            return res
        start, before = time.monotonic(), self.fetcher.thread_requests()
        slug = home.booking.rstrip("/").split("/")[-1].replace(".html", "")
        try:
            resp = self.fetcher.get(home.booking, key=f"booking/page/{slug}", headers=HONEST_UA)
            if is_bot_challenge(resp.status, resp.text):
                self.blocked_at = home.id
                res.status = Status.BLOCKED
                res.error = f"AWS WAF bot challenge (HTTP {resp.status}); stopped by design"
                return res
            if resp.status >= 400:
                res.status, res.error = Status.ERROR, f"HTTP {resp.status}"
                return res
            info = parse_json_ld(resp.text)
            if not info:
                res.status = Status.NO_RATINGS
                return res
            count = info["review_count"]
            res.metrics = [Metric("Overall rating", info["overall"])] + ([Metric("Review count", count)] if count is not None else [])
            res.status = Status.UNCHANGED if count is not None and count == previous_review_count else Status.METRICS_ONLY
        except BudgetExceeded as exc:
            res.status, res.error = Status.TIMEOUT, str(exc)
        except Exception as exc:
            res.status, res.error = Status.ERROR, f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            res.seconds = round(time.monotonic() - start, 2)
            res.requests = self.fetcher.thread_requests() - before
        return res
