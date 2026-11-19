import json

from review_monitor.http import Fetcher
from review_monitor.models import AIRBNB, Status
from review_monitor.sources.airbnb import AirbnbSource, StaleQuery, parse_listing_page, parse_reviews
from review_monitor.sources.booking import BookingSource, is_bot_challenge, parse_json_ld
from tests.helpers import AS_OF, CUTOFF, HOMES, ROOT


def body(key):
    return json.loads((ROOT / "fixtures" / f"{key}.json").read_text())["body"]


def test_parse_listing_page_reads_overall_count_categories_and_key():
    info = parse_listing_page(body("airbnb/page/47411844"))
    assert info["overall"] == 4.54 and info["review_count"] == 72
    assert list(info["categories"]) == ["Cleanliness", "Accuracy", "Check-in", "Communication", "Location", "Value"]
    assert info["categories"]["Check-in"] == 4.9
    assert info["api_key"] == "0" * 32


def test_parse_listing_page_rejects_blocked_or_changed_layout():
    import pytest
    with pytest.raises(ValueError):
        parse_listing_page("<html>challenge</html>")


def test_parse_reviews_finds_every_review():
    payload = json.loads(body("airbnb/reviews/1108914798187931072"))
    assert len(parse_reviews(payload)) == 14


def test_airbnb_collect_filters_to_window(fixtures):
    src = AirbnbSource(Fetcher("replay", fixtures))
    res = src.collect(HOMES[0], CUTOFF, previous_review_count=None)
    assert res.status == Status.OK
    assert [r.date for r in res.reviews] == ["2026-11-17", "2026-11-15", "2026-11-15"]
    assert all(r.platform == AIRBNB and r.id.startswith("airbnb-") for r in res.reviews)


def test_airbnb_skips_review_fetch_when_count_unchanged(fixtures):
    fetcher = Fetcher("replay", fixtures)
    res = AirbnbSource(fetcher).collect(HOMES[0], CUTOFF, previous_review_count=334)
    assert res.status == Status.UNCHANGED and res.requests == 1 and not res.reviews


def test_airbnb_one_bad_listing_is_an_error_not_a_crash(fixtures):
    (fixtures / "airbnb/page/1108914798187931072.json").write_text(json.dumps({"status": 200, "body": "<html></html>"}))
    res = AirbnbSource(Fetcher("replay", fixtures)).collect(HOMES[0], CUTOFF, None)
    assert res.status == Status.ERROR and "ValueError" in res.error


def test_airbnb_rediscovers_query_hash_when_stale(fixtures, monkeypatch):
    import review_monitor.sources.airbnb as mod
    calls = []

    def fake_fetch(fetcher, lid, key, qhash, cutoff_date=""):
        calls.append(qhash)
        if qhash == "old":
            raise StaleQuery(qhash)
        return []

    monkeypatch.setattr(mod, "fetch_reviews", fake_fetch)
    monkeypatch.setattr(mod, "discover_query_hash", lambda f, page: "new")
    src = AirbnbSource(Fetcher("replay", fixtures), query_hash="old")
    res = src.collect(HOMES[0], CUTOFF, None)
    assert calls == ["old", "new"] and src.query_hash == "new" and res.status == Status.OK_NO_RECENT


def test_booking_bot_challenge_is_detected_and_not_bypassed(fixtures):
    fetcher = Fetcher("replay", fixtures)
    src = BookingSource(fetcher)
    first, *rest = [src.collect(h, CUTOFF, None) for h in HOMES]
    assert first.status == Status.BLOCKED and "stopped by design" in first.error and first.requests == 1
    # after the first challenge the run stops asking: the other homes are reported blocked, never requested
    assert all(r.status == Status.BLOCKED and "not requested" in r.error for r in rest)
    assert fetcher.thread_requests() == 1 and not any(r.metrics for r in [first, *rest])


def test_booking_reads_json_ld_when_page_loads():
    html = '<script type="application/ld+json">{"@type":"Hotel","aggregateRating":{"ratingValue":"8.4","reviewCount":"212"}}</script>'
    assert parse_json_ld(html) == {"overall": 8.4, "review_count": 212}
    assert not is_bot_challenge(200, html)
    assert is_bot_challenge(202, "")


def test_a_booking_page_that_loads_reports_metrics_only_and_keeps_a_missing_count_empty():
    page = '<script type="application/ld+json">{"aggregateRating":{"ratingValue":"8.4"}}</script>'

    class Loads:
        def get(self, url, key, headers=None):
            from review_monitor.http import Response
            return Response(200, page, url)

        def thread_requests(self):
            return 1
    res = BookingSource(Loads()).collect(HOMES[0], CUTOFF, None)
    assert res.status == Status.METRICS_ONLY and [m.name for m in res.metrics] == ["Overall rating"]
