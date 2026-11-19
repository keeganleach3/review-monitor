"""Edge cases: review paging, changed response shapes, HTTP errors, tab retention, API retries."""
import json

import pytest

from review_monitor.agent import run_agent
from review_monitor.cli import run_once
from review_monitor.http import Response
from review_monitor.models import Status
from review_monitor.sheet import backends as be
from review_monitor.sources import airbnb
from review_monitor.sources.booking import BookingSource, is_bot_challenge
from tests.helpers import HOMES, make_tools, run_args


class PagedFetcher:
    """Serves review pages newest-first: 30 reviews in the window, then older ones."""
    def __init__(self, dates):
        self.dates, self.calls = dates, []

    def get(self, url, *, key, headers=None):
        self.calls.append(key)
        start = 24 * (int(key.split("-p")[1]) - 1) if "-p" in key else 0
        rows = [{"id": i, "createdAt": d, "comments": "ok"} for i, d in enumerate(self.dates[start:start + 24], start)]
        return Response(200, json.dumps({"data": {"reviews": {"reviews": rows}}}), "")


def test_reviews_paginate_until_past_the_window():
    dates = [f"2026-11-{18 - i // 5:02d}T10:00:00Z" for i in range(40)]  # 5 a day, newest first
    f = PagedFetcher(dates)
    out = airbnb.fetch_reviews(f, "1", None, "h", cutoff_date="2026-11-11")
    assert len(f.calls) == 2 and len(out) == 40          # page 2 fetched because page 1 was all in-window
    f2 = PagedFetcher(dates)
    airbnb.fetch_reviews(f2, "1", None, "h", cutoff_date="2026-11-16")
    assert len(f2.calls) == 1                            # page 1 already reached past the window


def test_changed_review_response_is_an_error_not_an_empty_week():
    with pytest.raises(ValueError):
        airbnb.parse_reviews({"data": {"somethingElse": {}}})


def test_booking_http_error_is_a_failure_not_blocked_or_empty():
    class F:
        def get(self, url, *, key, headers=None):
            return Response(500, "server error", "")
        def thread_requests(self):
            return 0
    res = BookingSource(F()).collect(HOMES[0], None, None)
    assert res.status == Status.ERROR and not is_bot_challenge(403, "plain forbidden page")


def test_old_run_tabs_are_pruned(tmp_path, fixtures, monkeypatch):
    from review_monitor.sheet import layout
    monkeypatch.setattr(layout, "KEEP_RUN_TABS", 2)
    xlsx = tmp_path / "out.xlsx"
    for _ in range(3):
        run_once(run_args(fixtures, xlsx))
    from openpyxl import load_workbook
    assert len([n for n in load_workbook(xlsx).sheetnames if n.startswith("Run ")]) == 2


def test_safety_net_tags_reviews_the_model_never_tagged(tmp_path, fixtures):
    class CollectsThenQuits:
        n = 0
        def create(self, **body):
            self.n += 1
            if self.n == 1:
                return {"stop_reason": "tool_use", "usage": {}, "content": [
                    {"type": "tool_use", "id": "a", "name": "collect_airbnb", "input": {}}]}
            return {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "done"}]}
    tools = make_tools(tmp_path, fixtures)
    run_agent(tools, client=CollectsThenQuits(), model="x")
    assert tools.written and tools.ctx.reviews and all(r["sentiment"] for r in tools.ctx.reviews)


def test_sheets_api_retries_on_rate_limit(monkeypatch):
    monkeypatch.setattr(be.time, "sleep", lambda s: None)
    class R:
        def __init__(self, code): self.status_code, self.text = code, "{}"
        def json(self): return {}
    class S:
        headers, codes = {}, [429, 429, 200]
        def request(self, *a, **k): return R(self.codes.pop(0))
    g = be.GoogleSheets("abc", session=S())
    assert g._req("GET", "https://x") == {}


def test_a_failed_review_fetch_is_retried_next_run_not_marked_unchanged(tmp_path):
    """If a run got a home's metrics but its review fetch failed, the next run must fetch reviews again.
    Trusting that run's review count would mark the home 'unchanged' and lose its reviews."""
    from review_monitor.sheet import Xlsx, load_prior
    x = Xlsx(tmp_path / "s.xlsx")
    x.reset_tab("Run 2026-11-18 10.00.00")
    x.write("Run 2026-11-18 10.00.00", 1, 1, [["Run 2026-11-18 10:00:00 UTC"]])  # complete-tab title
    x.write("Run 2026-11-18 10.00.00", 4, 1, [["Home", "Platform", "Metric", "Value", "Out of", "Status"],
                                              [1, "Airbnb", "Review count", 334, 5, Status.ERROR],
                                              [2, "Airbnb", "Review count", 219, 5, Status.OK]])
    prior, _ = load_prior(x)
    assert prior.review_counts == {(2, "Airbnb"): 219}


def test_guest_text_that_looks_like_a_formula_stays_text(tmp_path):
    from review_monitor.sheet import Xlsx
    x = Xlsx(tmp_path / "s.xlsx")
    x.reset_tab("T")
    x.write("T", 1, 1, [["=HYPERLINK(\"http://x\")", "2026-11-17"]], raw=True)
    x.save()
    assert Xlsx(tmp_path / "s.xlsx").read("T")[0] == ["=HYPERLINK(\"http://x\")", "2026-11-17"]
    assert Xlsx(tmp_path / "s.xlsx").wb["T"]["A1"].data_type == "s"


def test_a_failed_run_keeps_reviews_and_tags_so_they_are_not_new_again(tmp_path, fixtures):
    """A home that fails one run must not come back next run with its old reviews flagged as new
    (re-sent to the model, listed again in Changes)."""
    from review_monitor.agent import run_without_model
    from review_monitor.sheet import load_prior
    run_without_model(make_tools(tmp_path, fixtures))                    # run 1: everything tagged
    page = fixtures / "airbnb" / "page" / f"{HOMES[0].airbnb}.json"
    saved = page.read_text(encoding="utf-8")
    page.unlink()                                                        # run 2: home 1 fails
    prior, prev = load_prior(make_tools(tmp_path, fixtures).backend)
    failed = make_tools(tmp_path, fixtures, prior=prior, previous_tab=prev)
    run_without_model(failed)
    assert not failed.ctx.new_review_ids and len(failed.ctx.reviews) == 10   # home 1's reviews carried, not lost
    page.write_text(saved, encoding="utf-8")                             # run 3: home 1 recovers
    prior, prev = load_prior(make_tools(tmp_path, fixtures).backend)
    recovered = make_tools(tmp_path, fixtures, prior=prior, previous_tab=prev)
    run_without_model(recovered)
    assert not recovered.ctx.new_review_ids                              # nothing re-flagged as new


def test_a_run_that_cannot_write_the_sheet_still_alerts(tmp_path, fixtures, monkeypatch):
    from review_monitor import cli
    def sheet_down(*a, **k):
        raise RuntimeError("Sheets API 403: The caller does not have permission")
    monkeypatch.setattr(cli, "update_run_stats", sheet_down)
    with pytest.raises(RuntimeError):
        cli.cmd_run(run_args(fixtures, tmp_path / "out.xlsx"))
    assert "Sheets API 403" in (tmp_path / "alerts.log").read_text(encoding="utf-8")


def test_an_interrupted_publish_does_not_become_the_next_runs_memory(tmp_path, fixtures):
    """If a Sheets write fails partway through a run tab (metrics written, reviews not), the next run must
    read the last complete run tab, not the half-written one, or it would mark every home unchanged and
    carry forward no reviews."""
    from review_monitor.agent import run_without_model
    from review_monitor.sheet import Xlsx, load_prior
    run_without_model(make_tools(tmp_path, fixtures))                      # run 1: complete
    x = Xlsx(tmp_path / "out.xlsx")
    x.reset_tab("Run 2099-01-01 00.00.00", index=2)                         # run 2: died after the metrics block
    x.write("Run 2099-01-01 00.00.00", 4, 1, [["Home", "Platform", "Metric", "Value", "Out of", "Status"]] +
            [[h.id, "Airbnb", "Review count", 999, 5, Status.UNCHANGED] for h in HOMES], raw=True)
    x.save()
    prior, previous_tab = load_prior(Xlsx(tmp_path / "out.xlsx"))
    assert previous_tab != "Run 2099-01-01 00.00.00" and len(prior.reviews) == 10


def test_duplicate_reviews_are_collected_once_and_tagging_always_ends(tmp_path, fixtures):
    """Paging can return the same review twice (a new review shifts the offset). It must be stored once,
    and the fallback tagger must stop even if something can't be tagged."""
    from review_monitor.agent import _tag_remaining
    tools = make_tools(tmp_path, fixtures)
    tools.collect_airbnb()
    tools.ctx.reviews.append(dict(tools.ctx.reviews[0]))                     # a duplicate slips in anyway
    assert _tag_remaining(tools) >= 1                                       # returns instead of spinning
    assert all(r["sentiment"] for r in tools.ctx.reviews)


def test_unrecognized_airbnb_page_data_is_an_error_not_no_ratings():
    """If Airbnb renames its ratings data, every home must fail loudly (alert, carry forward), not look
    like a healthy listing with no ratings yet."""
    state = '{"niobeClientData": [["x", {"data": {"presentation": {"stayProductDetailPage": {"sections": []}}}}]]}'
    html = f'<script id="data-deferred-state-0" type="application/json">{state}</script>'
    with pytest.raises(ValueError):
        airbnb.parse_listing_page(html)
