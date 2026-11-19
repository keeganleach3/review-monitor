"""Airbnb: plain HTTP, no browser, no model.

The listing page embeds its rating data as JSON (`data-deferred-state-0`), and reviews come from
the same persisted GraphQL query the web app uses. Nothing here is secret: the public web key and
the query id are read from Airbnb's own page and JS bundles at runtime, so nothing sensitive is
committed, and the query id self-heals when Airbnb ships a new bundle.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import quote

from ..http import BudgetExceeded, Fetcher
from ..models import AIRBNB, JobResult, Metric, Review, Status

# Persisted-query id observed on 2026-11-18. Rediscovered automatically if Airbnb rotates it.
DEFAULT_QUERY_HASH = "cfdc3ffbe997a618795fc5a8f9a9b484054ce9be68c8788cd2ffda999934c5ae"
MUSCACHE = "https://a0.muscache.com/airbnb/static/packages/web/"


class StaleQuery(Exception):
    pass


def _walk(obj, pred, out):
    if isinstance(obj, dict):
        if pred(obj):
            out.append(obj)
        for v in obj.values():
            _walk(v, pred, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, pred, out)
    return out


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in str(v) else f
    except (TypeError, ValueError):
        return None


def parse_listing_page(html: str) -> dict:
    """Return {"overall", "review_count", "categories": {label: score}, "api_key"} from the page."""
    m = re.search(r'<script[^>]*id="data-deferred-state-0"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("no embedded listing state (layout changed, or blocked)")
    state = json.loads(m.group(1))
    found = _walk(state, lambda d: "categoryRatings" in d and "listingRatingStats" in d, [])
    if not found:  # the page loaded but its ratings data moved or was renamed: fail loudly, don't guess
        raise ValueError("listing ratings data not found in the page (Airbnb's page format may have changed)")
    q = found[0]
    overall = ((q.get("listingRatingStats") or {}).get("overallRatingStats") or {})
    result = {"overall": _num(overall.get("ratingAverage")), "review_count": _num(overall.get("ratingCount")),
              "categories": {}, "api_key": None}
    for c in q.get("categoryRatings") or []:
        score = _num(c.get("localizedRating"))
        if c.get("label") and score is not None:
            result["categories"][c["label"]] = score
    key = (re.search(r'"api_config":\{"key":"([a-z0-9]{32})"', html)
           or re.search(r'"key":"([a-z0-9]{32})"', html))
    result["api_key"] = os.environ.get("AIRBNB_WEB_API_KEY") or (key.group(1) if key else None)
    return result


def parse_reviews(payload: dict) -> list[dict]:
    """Every review object in the GraphQL response (they all carry createdAt + comments).

    Raises if the response has no reviews list at all, so a changed response shape shows up as an
    error instead of looking like "no reviews this week"."""
    if not _walk(payload, lambda d: isinstance(d.get("reviews"), list), []):
        raise ValueError("unexpected reviews response: no reviews list")
    return _walk(payload, lambda d: "createdAt" in d and ("comments" in d or "localizedReview" in d), [])


def discover_query_hash(fetcher: Fetcher, page_html: str) -> str | None:
    """Self-heal: find the current StaysPdpReviewsQuery id in Airbnb's JS bundles."""
    m = re.search(r'(https://a0\.muscache\.com/[^"\s]+?asyncRequire[^"\s]*?\.js)', page_html)
    if not m:
        return None
    index = fetcher.get(m.group(1), key="airbnb/bundles/asyncRequire").text
    paths = dict.fromkeys(re.findall(r'["\'](en/frontend/gp-stays-pdp[^"\']*[Rr]eview[^"\']*\.js)["\']', index))
    for i, path in enumerate(list(paths)[:15]):
        js = fetcher.get(MUSCACHE + path, key=f"airbnb/bundles/{i}").text
        hit = re.search(r"name:'StaysPdpReviewsQuery'[^}]*?operationId:'([a-f0-9]{64})'", js)
        if hit:
            return hit.group(1)
    return None


PAGE_SIZE, MAX_PAGES = 24, 5


def fetch_reviews(fetcher: Fetcher, listing_id: str, api_key: str | None, query_hash: str,
                  cutoff_date: str = "", limit: int = PAGE_SIZE) -> list[dict]:
    """Newest-first pages of reviews, stopping once a page reaches past the 7-day window."""
    out = []
    for page in range(MAX_PAGES):
        batch = _fetch_page(fetcher, listing_id, api_key, query_hash, offset=page * limit, limit=limit)
        out += batch
        if len(batch) < limit or not batch or str(batch[-1].get("createdAt", ""))[:10] <= cutoff_date:
            break
    return out


def _fetch_page(fetcher: Fetcher, listing_id: str, api_key: str | None, query_hash: str,
                offset: int, limit: int) -> list[dict]:
    gid = base64.b64encode(f"StayListing:{listing_id}".encode()).decode()
    variables = {"id": gid, "pdpReviewsRequest": {
        "fieldSelector": "for_p3_translation_only", "forPreview": False, "limit": limit,
        "offset": str(offset), "showingTranslationButton": False, "first": limit,
        "sortingPreference": "MOST_RECENT"}}
    ext = {"persistedQuery": {"version": 1, "sha256Hash": query_hash}}
    url = (f"https://www.airbnb.com/api/v3/StaysPdpReviewsQuery/{query_hash}"
           f"?operationName=StaysPdpReviewsQuery&locale=en&currency=USD"
           f"&variables={quote(json.dumps(variables))}&extensions={quote(json.dumps(ext))}")
    headers = {"X-Airbnb-API-Key": api_key} if api_key else {}
    key = f"airbnb/reviews/{listing_id}" + (f"-p{offset // limit + 1}" if offset else "")
    resp = fetcher.get(url, key=key, headers=headers)
    if "PersistedQueryNotFound" in resp.text:
        raise StaleQuery(query_hash)
    return parse_reviews(resp.json())


class AirbnbSource:
    platform = AIRBNB

    def __init__(self, fetcher: Fetcher, query_hash: str | None = None):
        self.fetcher = fetcher
        self.query_hash = query_hash or os.environ.get("AIRBNB_REVIEWS_QUERY_HASH") or DEFAULT_QUERY_HASH

    def collect(self, home, cutoff: datetime, previous_review_count: int | None) -> JobResult:
        res = JobResult(home=home.id, platform=AIRBNB)
        start, before = time.monotonic(), self.fetcher.thread_requests()
        try:
            page = self.fetcher.get(f"https://www.airbnb.com/rooms/{home.airbnb}?locale=en&currency=USD",
                                    key=f"airbnb/page/{home.airbnb}").text
            info = parse_listing_page(page)
            if info["overall"] is None and not info["categories"]:
                res.status = Status.NO_RATINGS
                return res
            res.metrics.append(Metric("Overall rating", info["overall"]))
            res.metrics.append(Metric("Review count", info["review_count"]))
            res.metrics += [Metric(label, score) for label, score in info["categories"].items()]

            # Skip when nothing changed: same review count means no new reviews to fetch or tag.
            if previous_review_count is not None and info["review_count"] == previous_review_count:
                res.status = Status.UNCHANGED
                return res

            try:
                raw = fetch_reviews(self.fetcher, home.airbnb, info["api_key"], self.query_hash, cutoff.date().isoformat())
            except StaleQuery:
                new_hash = discover_query_hash(self.fetcher, page)
                if not new_hash:
                    raise
                self.query_hash = new_hash
                raw = fetch_reviews(self.fetcher, home.airbnb, info["api_key"], self.query_hash, cutoff.date().isoformat())

            for rv in raw:
                try:
                    created = datetime.fromisoformat(str(rv["createdAt"]).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if created.date() <= cutoff.date():  # window = the last 7 calendar days (same rule as carry-forward)
                    continue
                text = rv.get("comments") or (rv.get("localizedReview") or {}).get("comments") or ""
                reviewer = (rv.get("reviewer") or {}).get("firstName") or ""
                res.reviews.append(Review(
                    id=f"airbnb-{rv.get('id')}", home=home.id, platform=AIRBNB,
                    date=created.date().isoformat(), rating=_num(rv.get("rating")),
                    reviewer=reviewer, text=re.sub(r"<br\s*/?>", " ", text).strip()[:4000]))
            res.status = Status.OK if res.reviews else Status.OK_NO_RECENT
        except BudgetExceeded as exc:
            res.status, res.error = Status.TIMEOUT, str(exc)
        except Exception as exc:  # one bad listing must never fail the run
            res.status, res.error = Status.ERROR, f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            res.seconds = round(time.monotonic() - start, 2)
            res.requests = self.fetcher.thread_requests() - before
        return res

