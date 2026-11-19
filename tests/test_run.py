"""End-to-end runs (replay data -> local workbook), checked against the brief's requirements."""
import json

from openpyxl import load_workbook

from review_monitor import alerts
from review_monitor.cli import run_once
from review_monitor.models import JobResult, Status
from tests.helpers import run_args


def tabs(path):
    return load_workbook(path).sheetnames


def cells(path, tab):
    return [list(r) for r in load_workbook(path)[tab].iter_rows(values_only=True)]


def test_first_run_meets_the_brief(tmp_path, fixtures):
    xlsx = tmp_path / "out.xlsx"
    ctx = run_once(run_args(fixtures, xlsx))
    names = tabs(xlsx)
    assert names[:2] == ["Dashboard", "Changes"] and names[2].startswith("Run ") and len(names) == 3

    rows = cells(xlsx, names[2])
    review_hdr = next(i for i, r in enumerate(rows) if r[0] == "Home" and r[2] == "Date")
    metric = [r for r in rows[4:review_hdr] if r[0] == 1 and r[1] == "Airbnb"]
    assert [r[2] for r in metric] == ["Overall rating", "Review count", "Cleanliness", "Accuracy",
                                      "Check-in", "Communication", "Location", "Value"]  # every category shown
    assert metric[0][3] == 4.66 and metric[1][3] == 334 and metric[0][4] == 5
    booking = [r for r in rows if r[1] == "Booking.com" and r[5]]
    assert len(booking) == 10 and all(r[5] == Status.BLOCKED for r in booking)
    reviews = [r for r in rows[review_hdr + 1:] if r[11]]
    assert len(reviews) == 10 and all(r[5] and r[2] and r[3] and r[6] for r in reviews)  # reviewer, date, rating, text
    assert all(r[2] > "2026-11-11" for r in reviews)                                      # last 7 days only
    assert ctx.alerts == []                       # Booking's wall alone is expected, not an alert


def test_dashboard_uses_formulas_over_the_latest_run(tmp_path, fixtures):
    xlsx = tmp_path / "out.xlsx"
    run_once(run_args(fixtures, xlsx))
    dash = cells(xlsx, "Dashboard")
    header = dash[6]
    assert header[1:6] == ["Home", "Rating", "Reviews", "Rating", "Reviews"]
    row1 = dash[7]
    latest = tabs(xlsx)[2]
    assert row1[1] == 1 and str(row1[2]).startswith("=IF(COUNTIFS(") and latest in row1[2]
    assert row1[6] == '=IF(C8="","",C8/5)' and row1[7] == '=IF(E8="","",E8/10)'   # % of max: sites on their own scales
    # the onsite bug was formulas that didn't fill down (homes 2-10 blank): every home gets its own formulas
    for i, row in enumerate(dash[7:17]):
        assert row[1] == i + 1 and f"$B{8 + i}" in row[2] and row[6] == f'=IF(C{8 + i}="","",C{8 + i}/5)'


def test_second_run_keeps_previous_tab_and_skips_unchanged_work(tmp_path, fixtures):
    xlsx = tmp_path / "out.xlsx"
    run_once(run_args(fixtures, xlsx))
    ctx = run_once(run_args(fixtures, xlsx))
    names = tabs(xlsx)
    assert len([n for n in names if n.startswith("Run ")]) == 2 and names[2] == ctx.tab_name  # newest first
    assert {j.status for j in ctx.jobs if j.platform == "Airbnb"} == {Status.UNCHANGED}
    assert sum(j.requests for j in ctx.jobs) == 11           # 10 Airbnb pages + 1 Booking page, no review fetches
    assert len(ctx.reviews) == 10 and not ctx.new_review_ids  # reviews carried forward, nothing re-tagged
    assert all(r["sentiment"] for r in ctx.reviews)           # tags carried forward too
    assert cells(xlsx, "Changes")[4][2] == "No metric changed"


def test_changes_tab_shows_what_moved(tmp_path, fixtures):
    """Synthetic day 2 for home 3 (test data, not real): one new 2-star review, count 163 -> 164."""
    xlsx = tmp_path / "out.xlsx"
    run_once(run_args(fixtures, xlsx))
    page = fixtures / "airbnb/page/1369819248348941727.json"
    d = json.loads(page.read_text())
    d["body"] = d["body"].replace('"ratingCount": "163"', '"ratingCount": "164"').replace('"ratingAverage": 4.63', '"ratingAverage": 4.62')
    page.write_text(json.dumps(d))
    rev = fixtures / "airbnb/reviews/1369819248348941727.json"
    d = json.loads(rev.read_text())
    payload = json.loads(d["body"])
    payload["data"]["presentation"]["stayProductDetailPage"]["reviews"]["reviews"].insert(0, {
        "id": "999", "comments": "Loud street noise all night and the wifi kept dropping.",
        "createdAt": "2026-11-18T09:00:00Z", "rating": 2, "reviewer": {"firstName": "Test"}})
    d["body"] = json.dumps(payload)
    rev.write_text(json.dumps(d))

    ctx = run_once(run_args(fixtures, xlsx))
    assert ctx.new_review_ids == {"airbnb-999"}
    ch = cells(xlsx, "Changes")
    moved = [(r[0], r[2]) for r in ch[4:] if r[0]]
    assert moved == [(3, "Overall rating"), (3, "Review count")]
    assert any(r[12] and "wifi kept dropping" in r[12] for r in ch[4:])
    new = next(r for r in ctx.reviews if r["review_id"] == "airbnb-999")
    assert new["sentiment"] == "negative" and set(new["complaints"].split(", ")) == {"Noise", "WiFi"}


def test_run_budget_stops_cleanly_and_alerts(tmp_path, fixtures):
    ctx = run_once(run_args(fixtures, tmp_path / "out.xlsx", budget=0))
    assert all(j.status in Status.FAILED for j in ctx.jobs)
    assert ctx.alerts and (tmp_path / "alerts.log").exists()


def test_alert_rules():
    ok = [JobResult(i, "Airbnb", Status.OK) for i in range(10)]
    blocked = [JobResult(i, "Booking.com", Status.BLOCKED) for i in range(10)]
    assert alerts.evaluate(ok + blocked) == []                    # the expected bot wall doesn't page anyone
    worse = ok[:3] + [JobResult(i, "Airbnb", Status.ERROR) for i in range(7)]
    assert any("7/10" in r for r in alerts.evaluate(worse))
    # 6 of 10 Airbnb homes failing is an outage, even when Booking's 10 expected blocks share the run
    half_down = ok[:4] + [JobResult(i, "Airbnb", Status.ERROR) for i in range(6)] + blocked
    assert any("6/" in r for r in alerts.evaluate(half_down))
    # one flaky listing is not worth paging anyone over
    assert alerts.evaluate(ok[:9] + [JobResult(9, "Airbnb", Status.ERROR)] + blocked) == []
