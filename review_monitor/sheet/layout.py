"""What the spreadsheet looks like. Three kinds of tab, nothing else:

  Dashboard   one page; every number is a formula over the latest run tab (each home compared across sites)
  Changes     metrics that moved since the previous run (formulas) and reviews that are new since then
  Run <time>  one per run, newest first, earlier runs kept: this run's metrics, then its reviews

The latest run tab is also the state for the next run (last values, review ids and their tags), so there
are no hidden data tabs.
"""
from __future__ import annotations

import os

from ..models import AIRBNB, BOOKING, SCALE, Status
from ..tagging import TAXONOMY
from .backends import TabStyle

NAVY, LIGHT, GREY, MUTED, WHITE = "#1F3A5F", "#EEF2F7", "#5F6B7A", "#8A8F98", "#FFFFFF"
HEADER = {"bold": True, "fg": WHITE, "bg": NAVY, "wrap": "wrap", "valign": "middle"}
SECTION = {"bold": True, "size": 12, "fg": NAVY}
STATUS_RULES = [  # contains-text -> (text, fill); first match wins
    ("blocked", "#8A5A00", "#FFF4D6"), ("error", "#A32020", "#FBE3E3"), ("timeout", "#A32020", "#FBE3E3"),
    ("skipped", "#A32020", "#FBE3E3"), ("unchanged", "#1F6B3A", "#E3F4E8"), ("ok", "#1F6B3A", "#E3F4E8"),
]

DASHBOARD, CHANGES, RUN_PREFIX = "Dashboard", "Changes", "Run "
COMPLETE_MARK = "Run "  # the run tab's title, written after its data

# Run tab: metrics block (A:F) on top, reviews block (A:L) below it. Same columns for both, so the
# Dashboard can read each block by column: metric rows have a Metric in C; review rows have an id in L.
M_COLS = ["Home", "Platform", "Metric", "Value", "Out of", "Status"]
R_COLS = ["Home", "Platform", "Date", "Rating", "Out of", "Reviewer", "Review", "Sentiment",
          "Praise", "Complaints", "New this run", "Review ID"]
M_HDR = 4  # metrics header row
KEEP_RUN_TABS = 72  # 12 hours at a 10-minute cadence; KEEP_RUN_TABS in the environment overrides it


def _status_rules(r1, c1, r2, c2):
    return [(r1, c1, r2, c2, t, fg, bg) for t, fg, bg in STATUS_RULES]


# ------------------------------------------------------------------------------------------------
def load_prior(backend):
    """Read the newest complete run tab back: last metric values and statuses, review counts, review ids + tags.

    A run tab's title cell is written last, so a tab without it was interrupted mid-write (for example by a
    Sheets error) and is skipped: trusting its counts without its reviews would lose the reviews."""
    from ..collect import PriorState
    prior = PriorState()
    for latest in sorted((t for t in backend.tab_names() if t.startswith(RUN_PREFIX)), reverse=True):
        rows = backend.read(latest)
        if rows and rows[0] and str(rows[0][0]).startswith(COMPLETE_MARK):
            break
    else:
        return prior, None
    mode = None
    for row in rows:
        row = (list(row) + [None] * len(R_COLS))[:len(R_COLS)]
        if row[0] == "Home":
            mode = "reviews" if row[2] == "Date" else "metrics"
            continue
        try:
            home = int(row[0])
        except (TypeError, ValueError):
            continue
        platform = row[1]
        if mode == "metrics":
            prior.statuses[(home, platform)] = row[5]
            if row[3] not in (None, ""):
                prior.metrics[(home, platform, row[2])] = float(row[3])
                # Only a healthy job's count may mark the next run "unchanged": if this run's review fetch
                # failed, any new reviews were never fetched, and skipping the refetch would miss them.
                if row[2] == "Review count" and row[5] in Status.HEALTHY:
                    prior.review_counts[(home, platform)] = int(float(row[3]))
        elif mode == "reviews" and row[11]:
            stored = {"review_id": row[11], "home": home, "platform": platform, "date": str(row[2])[:10],
                      "rating": row[3], "scale": row[4], "reviewer": row[5], "text": row[6],
                      "sentiment": row[7] or "", "praise": row[8] or "", "complaints": row[9] or ""}
            prior.reviews[stored["review_id"]] = stored
            prior.last_window_reviews.setdefault((home, platform), []).append(stored)
    return prior, latest


# ------------------------------------------------------------------------------------------------
def run_line(ctx, n_reviews: int) -> str:
    a = sum(1 for j in ctx.jobs if j.platform == AIRBNB and j.status in Status.HEALTHY)
    b_blocked = sum(1 for j in ctx.jobs if j.platform == BOOKING and j.status == Status.BLOCKED)
    b_ok = sum(1 for j in ctx.jobs if j.platform == BOOKING and j.status in Status.HEALTHY)
    n = len(ctx.homes)
    booking = f"{b_blocked}/{n} blocked by bot protection (reported, not bypassed)" if b_blocked else f"{b_ok}/{n} collected"
    return (f"Airbnb: {a}/{n} collected · Booking.com: {booking} · {n_reviews} reviews in the last {ctx.window_days} days "
            f"({len(ctx.new_review_ids)} new) · {ctx.seconds} s · {sum(j.requests for j in ctx.jobs)} requests · "
            f"model: {ctx.model or 'none'}, {ctx.input_tokens:,} in / {ctx.output_tokens:,} out tokens")


def dash_line(ctx) -> str:
    return (f"Latest run: {ctx.tab_name}   ·   previous run: {ctx.previous_tab or 'none yet'}   ·   this run: {ctx.seconds} s, "
            f"{sum(j.requests for j in ctx.jobs)} requests, {ctx.input_tokens + ctx.output_tokens:,} model tokens   ·   model: {ctx.model or 'none'}")


def update_run_stats(ctx, backend) -> None:
    """After the agent finishes, stamp the final time and token counts (the run tab is written mid-loop)."""
    backend.write(ctx.tab_name, 2, 1, [[run_line(ctx, len(ctx.reviews))]])
    backend.write(DASHBOARD, 3, 2, [[dash_line(ctx)]])
    backend.save()


def run_tab(ctx, backend) -> str:
    name = f"{RUN_PREFIX}{ctx.run_label.replace(' UTC', '').replace(':', '.')}"  # ':' isn't allowed in tab names
    existing, base, n = set(backend.tab_names()), name, 2
    while name in existing:  # two runs inside the same second (tests, retries)
        name, n = f"{base} #{n}", n + 1
    st = TabStyle(freeze_rows=M_HDR, tab_color="#2E7D5B",
                  widths={1: 52, 2: 92, 3: 150, 4: 62, 5: 52, 6: 200, 7: 460, 8: 80, 9: 210, 10: 170, 11: 70, 12: 190})
    backend.reset_tab(name, index=2, style=st)

    metric_rows = []
    by_job = {(j.home, j.platform): j for j in ctx.jobs}
    for h in ctx.homes:
        for platform in (AIRBNB, BOOKING):
            j = by_job.get((h.id, platform))
            if not j:
                continue
            if j.metrics:
                metric_rows += [[h.id, platform, m.name, m.value, SCALE[platform], j.status] for m in j.metrics]
            else:
                why = "bot challenge" if j.status == Status.BLOCKED else (j.error or j.status)[:60]
                metric_rows.append([h.id, platform, f"not collected ({why})", "", SCALE[platform], j.status])

    reviews = sorted(ctx.reviews, key=lambda r: (r["date"], -r["home"]), reverse=True)
    review_rows = [[r["home"], r["platform"], r["date"], r["rating"], r["scale"], r["reviewer"], r["text"],
                    r["sentiment"], r["praise"], r["complaints"],
                    "new" if r["review_id"] in ctx.new_review_ids else "", r["review_id"]] for r in reviews]

    w = backend.write
    w(name, 2, 1, [[run_line(ctx, len(reviews))]])
    w(name, 3, 1, [["Review metrics (each site's own scale and categories)"]])
    w(name, M_HDR, 1, [M_COLS])
    w(name, M_HDR + 1, 1, metric_rows, raw=True)
    r_title = M_HDR + 1 + len(metric_rows) + 1
    r_hdr = r_title + 1
    w(name, r_title, 1, [[f"Reviews posted in the last {ctx.window_days} days ({len(reviews)})"]])
    w(name, r_hdr, 1, [R_COLS])
    w(name, r_hdr + 1, 1, review_rows or [["", "", "", "", "", "", "No reviews in the window."]], raw=True)
    w(name, 1, 1, [[COMPLETE_MARK + ctx.run_label]])  # written last: load_prior trusts only tabs that have it
    last_m, last_r = M_HDR + len(metric_rows), r_hdr + max(1, len(review_rows))

    st.formats = [
        (1, 1, 1, 1, {"bold": True, "size": 14, "fg": NAVY}),
        (2, 1, 2, 1, {"italic": True, "fg": GREY}),
        (3, 1, 3, 1, SECTION), (r_title, 1, r_title, 1, SECTION),
        (M_HDR, 1, M_HDR, len(M_COLS), HEADER), (r_hdr, 1, r_hdr, len(R_COLS), HEADER),
        (M_HDR + 1, 1, last_m, 1, {"halign": "left"}), (r_hdr + 1, 1, last_r, 1, {"halign": "left"}),
        (M_HDR + 1, 5, last_r, 5, {"halign": "center"}), (r_hdr + 1, 4, last_r, 4, {"halign": "center"}),
        (r_hdr + 1, 1, last_r, len(R_COLS), {"valign": "top"}),
        (r_hdr + 1, 7, last_r, 7, {"wrap": "wrap"}), (r_hdr + 1, 9, last_r, 10, {"wrap": "wrap"}),
        (r_hdr + 1, 12, last_r, 12, {"fg": MUTED, "size": 8}),
    ]
    st.conditional = _status_rules(M_HDR + 1, 6, last_m, 6) + [(r_hdr + 1, 11, last_r, 11, "new", NAVY, "#E3ECF7")]
    backend.style(name, st)
    return name


# ------------------------------------------------------------------------------------------------
def _ref_for(tab: str):
    """Whole column of a run tab. The Dashboard is rebuilt every run, so it points straight at the latest tab."""
    return lambda col: f"'{tab}'!${col}:${col}"


def _metric(ref, platform: str, metric: str, r: int) -> str:
    crit = f'{ref("A")},$B{r},{ref("B")},"{platform}",{ref("C")},"{metric}"'
    return f'=IF(COUNTIFS({crit})=0,"",SUMIFS({ref("D")},{crit}))'


def dashboard(ctx, backend, latest_tab: str) -> None:
    st = TabStyle(hide_grid=True, tab_color=NAVY,
                  widths={1: 18, 2: 56, 3: 128, 4: 124, 5: 110, 6: 112, 7: 112, 8: 112, 9: 70, 10: 90,
                          11: 80, 12: 70, 13: 68, 14: 72, 15: 120, 16: 205})
    backend.reset_tab(DASHBOARD, index=0, style=st)
    ref = _ref_for(latest_tab)
    w = backend.write
    w(DASHBOARD, 1, 2, [["Guest Review Monitor"]])
    w(DASHBOARD, 2, 2, [["10 homes on Airbnb (rated out of 5) and Booking.com (out of 10). Each site keeps its own scale and "
                         "categories; the % of max columns are only for comparing across sites. Every number below is a formula over the latest run tab."]])
    w(DASHBOARD, 3, 2, [[dash_line(ctx)]])

    T0 = 7
    first, last, n = T0 + 1, T0 + len(ctx.homes), len(ctx.homes)
    kpis = [("Airbnb homes collected", f'=COUNTIF(O{first}:O{last},"ok*")+COUNTIF(O{first}:O{last},"unchanged")&" / {n}"'),
            ("Booking.com homes collected", f'=IF(COUNTIF(P{first}:P{last},"blocked*")={n},"Blocked",COUNTIF(P{first}:P{last},"ok*")+COUNTIF(P{first}:P{last},"unchanged")&" / {n}")'),
            ("Avg Airbnb rating (/5)", f'=IFERROR(ROUND(AVERAGE(C{first}:C{last}),2),"n/a")'),
            ("Avg Booking rating (/10)", f'=IFERROR(ROUND(AVERAGE(E{first}:E{last}),1),"n/a")'),
            ("Reviews seen, last 7 days", f"=SUM(M{first}:N{last})"),
            ("Metrics changed since last run", f'=COUNTIF(Changes!G:G,"up")+COUNTIF(Changes!G:G,"down")')]
    w(DASHBOARD, 4, 3, [[k for k, _ in kpis]])
    w(DASHBOARD, 5, 3, [[v for _, v in kpis]])

    w(DASHBOARD, T0 - 1, 3, [["Airbnb (out of 5)", "", "Booking.com (out of 10)", "", "Compared (% of max)", "", "",
                              "Airbnb categories", "", "", "Reviews, last 7 days", "", "Collection status"]])
    w(DASHBOARD, T0, 2, [["Home", "Rating", "Reviews", "Rating", "Reviews", "Airbnb", "Booking", "Gap",
                          "Cleanliness", "Location", "Value", "Airbnb", "Booking", "Airbnb", "Booking.com"]])
    rows = []
    for i, h in enumerate(ctx.homes):
        r = first + i
        # status: first metric row for this home + platform (metric rows carry the site's scale in E)
        status = lambda p, r=r: (f'=IFERROR(INDEX({ref("F")},MATCH(1,INDEX(({ref("A")}=$B{r})*({ref("B")}="{p}")'
                                 f'*({ref("L")}=""),0),0)),"")')
        # A count only when that site was actually read this run: a blocked or failed site shows blank, not 0.
        recent = lambda p, st, r=r: (f'=IF(OR(LEFT({st}{r},2)="ok",{st}{r}="unchanged",{st}{r}="no_ratings_yet"),'
                                     f'COUNTIFS({ref("A")},$B{r},{ref("B")},"{p}",{ref("L")},"<>"),"")')
        rows.append([h.id, _metric(ref, AIRBNB, "Overall rating", r), _metric(ref, AIRBNB, "Review count", r),
                     _metric(ref, BOOKING, "Overall rating", r), _metric(ref, BOOKING, "Review count", r),
                     f'=IF(C{r}="","",C{r}/5)', f'=IF(E{r}="","",E{r}/10)', f'=IF(OR(G{r}="",H{r}=""),"",G{r}-H{r})',
                     _metric(ref, AIRBNB, "Cleanliness", r), _metric(ref, AIRBNB, "Location", r),
                     _metric(ref, AIRBNB, "Value", r),
                     recent(AIRBNB, "O"), recent(BOOKING, "P"), status(AIRBNB), status(BOOKING)])
    w(DASHBOARD, first, 2, rows)
    w(DASHBOARD, last + 1, 2, [["Avg", f'=IFERROR(AVERAGE(C{first}:C{last}),"")', "", f'=IFERROR(AVERAGE(E{first}:E{last}),"")', "",
                                f'=IFERROR(AVERAGE(G{first}:G{last}),"")', f'=IFERROR(AVERAGE(H{first}:H{last}),"")']])

    th = last + 4
    w(DASHBOARD, th, 2, [["What guests praise and complain about (reviews in the latest run tab)"]])
    w(DASHBOARD, th + 1, 3, [["Theme", "Group", "Praise", "Complaints"]])
    trows = [[t, g, f'=COUNTIF({ref("I")},"*{t}*")', f'=COUNTIF({ref("J")},"*{t}*")'] for t, g in TAXONOMY.items()]
    w(DASHBOARD, th + 2, 3, trows)
    key = th + 2 + len(trows) + 1
    w(DASHBOARD, key, 2, [["Status key"], ["ok: collected"], ["ok_no_recent_reviews: collected, no reviews in the last 7 days"],
                          ["ok_metrics_only: rating read, but this source can't see reviews"],
                          ["unchanged: review count unchanged since the last run, so reviews were carried forward instead of refetched"],
                          ["blocked_by_bot_protection: Booking.com's bot wall. Expected; reported every run and not bypassed"],
                          ["timeout / error / skipped_run_budget: failed; over 30% failed on one site sends an alert"]])

    st.formats = [
        (1, 2, 1, 2, {"bold": True, "size": 18, "fg": NAVY}),
        (2, 2, 2, 2, {"italic": True, "fg": GREY}), (3, 2, 3, 2, {"fg": GREY, "size": 9}),
        (4, 3, 4, 8, {"size": 9, "bold": True, "fg": GREY, "bg": LIGHT, "wrap": "wrap", "valign": "top"}),
        (5, 3, 5, 8, {"size": 15, "bold": True, "fg": NAVY, "bg": LIGHT, "halign": "left"}),
        (T0 - 1, 3, T0 - 1, 16, {"bold": True, "fg": NAVY, "size": 9}),
        (T0, 2, T0, 16, HEADER),
        (first, 2, last + 1, 16, {"valign": "middle"}), (first, 2, last, 2, {"halign": "center"}),
        (first, 3, last + 1, 3, {"numfmt": "0.00", "bold": True}), (first, 5, last + 1, 5, {"numfmt": "0.0", "bold": True}),
        (first, 7, last + 1, 8, {"numfmt": "0%"}), (first, 9, last + 1, 9, {"numfmt": "+0%;-0%;0%"}),
        (first, 10, last, 12, {"numfmt": "0.0"}), (first, 13, last, 14, {"halign": "center"}),
        (last + 1, 2, last + 1, 9, {"bold": True, "fg": GREY}),
        (th, 2, th, 2, SECTION), (th + 1, 3, th + 1, 6, HEADER), (th + 2, 5, th + 1 + len(trows), 6, {"halign": "center"}),
        (key, 2, key, 2, {"bold": True, "fg": GREY, "size": 9}), (key + 1, 2, key + 5, 2, {"fg": GREY, "size": 9}),
    ]
    st.conditional = _status_rules(first, 15, last, 16)
    backend.style(DASHBOARD, st)


# ------------------------------------------------------------------------------------------------
def changes(ctx, backend, latest_tab: str, previous_tab: str | None) -> None:
    st = TabStyle(freeze_rows=4, tab_color=NAVY, hide_grid=True,
                  widths={1: 52, 2: 92, 3: 230, 4: 76, 5: 76, 6: 70, 7: 80, 8: 26, 9: 52, 10: 92, 11: 90, 12: 56, 13: 460})
    backend.reset_tab(CHANGES, index=1, style=st)
    w = backend.write
    current = {(j.home, j.platform, m.name): m.value for j in ctx.jobs for m in j.metrics if m.value is not None}
    moved = sorted(k for k, v in current.items() if k in ctx.prior.metrics and ctx.prior.metrics[k] != float(v))
    status_changes = []
    for j in ctx.jobs:
        before = ctx.prior.statuses.get((j.home, j.platform))
        quiet = {Status.OK, Status.OK_NO_RECENT, Status.UNCHANGED}
        if previous_tab and before and before != j.status and not {before, j.status} <= quiet:
            status_changes.append((j.home, j.platform, before, j.status))

    w(CHANGES, 1, 1, [["What changed since the previous run"]])
    w(CHANGES, 2, 1, [[f"Latest: {latest_tab}   ·   previous: {previous_tab or 'none yet (first run)'}   ·   "
                       f"{len(moved)} of {len(current)} metrics moved (values are formulas over the two run tabs)"]])
    w(CHANGES, 3, 1, [["Metrics that moved"]])
    w(CHANGES, 3, 9, [["Reviews that are new since the previous run"]])
    w(CHANGES, 4, 1, [["Home", "Platform", "Metric", "Previous", "Latest", "Change", "Direction"]])
    w(CHANGES, 4, 9, [["Home", "Platform", "Date", "Rating", "Review"]])

    def val(tab, r):
        q = lambda c: f"'{tab}'!${c}:${c}"
        crit = f"{q('A')},$A{r},{q('B')},$B{r},{q('C')},$C{r}"
        return f'=IF(COUNTIFS({crit})=0,"",SUMIFS({q("D")},{crit}))'

    rows = []
    for i, (home, platform, metric) in enumerate(moved):
        r = 5 + i
        rows.append([home, platform, metric, val(previous_tab, r), val(latest_tab, r), f'=IF(OR(D{r}="",E{r}=""),"",E{r}-D{r})',
                     f'=IF(F{r}="","",IF(F{r}>0,"up",IF(F{r}<0,"down","")))'])
    for home, platform, before, after in status_changes:
        rows.append([home, platform, f"status: {before} → {after}", "", "", "", ""])
    if not rows:
        rows = [["", "", "No metric changed" if previous_tab else "First run: nothing to compare yet", "", "", "", ""]]
    w(CHANGES, 5, 1, rows)

    new = [r for r in ctx.reviews if r["review_id"] in ctx.new_review_ids] if previous_tab else []
    new_rows = [[r["home"], r["platform"], r["date"], r["rating"], r["text"]] for r in sorted(new, key=lambda r: r["date"], reverse=True)]
    w(CHANGES, 5, 9, new_rows or [["", "", "", "", "None" if previous_tab else "First run: every review is new (see the run tab)"]],
      raw=True)
    last = 4 + max(len(rows), len(new_rows), 1)
    st.formats = [
        (1, 1, 1, 1, {"bold": True, "size": 14, "fg": NAVY}), (2, 1, 2, 1, {"italic": True, "fg": GREY}),
        (3, 1, 3, 9, SECTION), (4, 1, 4, 7, HEADER), (4, 9, 4, 13, HEADER),
        (5, 1, last, 1, {"halign": "left"}), (5, 9, last, 9, {"halign": "left"}),
        (5, 6, last, 6, {"numfmt": "+0.0#;-0.0#;0"}), (5, 1, last, 13, {"valign": "top"}), (5, 13, last, 13, {"wrap": "wrap"}),
    ]
    st.conditional = [(5, 7, last, 7, "up", "#1F6B3A", "#E3F4E8"), (5, 7, last, 7, "down", "#A32020", "#FBE3E3")]
    backend.style(CHANGES, st)


def publish(ctx, backend, previous_tab: str | None) -> str:
    """Write this run: new run tab (earlier ones kept), then Changes and the Dashboard rebuilt over it."""
    latest = run_tab(ctx, backend)
    ctx.tab_name, ctx.sheet_url, ctx.previous_tab = latest, backend.url, previous_tab
    changes(ctx, backend, latest, previous_tab)
    dashboard(ctx, backend, latest)
    backend.move(latest, 2)  # newest run first, right after Dashboard and Changes
    runs = sorted((t for t in backend.tab_names() if t.startswith(RUN_PREFIX)), reverse=True)
    for old in runs[int(os.environ.get("KEEP_RUN_TABS", KEEP_RUN_TABS)):]:  # keep the latest N run tabs so a 10-minute schedule can't fill the workbook
        backend.delete_tab(old)
    if previous_tab is None and "Sheet1" in backend.tab_names() and not backend.read("Sheet1"):
        backend.delete_tab("Sheet1")  # a new Google Sheet's empty default tab
    backend.save()
    return latest
