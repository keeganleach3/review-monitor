"""Run state and the platform collectors (code, not model).

One job per (home, platform). Each platform runs in its own worker pool with its own concurrency, every
job has a timeout, and the run has a time budget for collection. Runs are sequential, so a 10-minute schedule
never overlaps itself.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .http import Budget
from .models import JobResult, Status

PLATFORM_CONCURRENCY = {"Airbnb": 3, "Booking.com": 1}
JOB_TIMEOUT = 90.0


@dataclass
class PriorState:
    """What the Sheet already holds from earlier runs."""
    review_counts: dict = field(default_factory=dict)      # (home, platform) -> int, from the latest run
    metrics: dict = field(default_factory=dict)            # (home, platform, metric) -> value, from the latest run
    statuses: dict = field(default_factory=dict)           # (home, platform) -> status, from the latest run
    reviews: dict = field(default_factory=dict)            # review_id -> stored row (incl. tags)
    last_window_reviews: dict = field(default_factory=dict)  # (home, platform) -> [review rows] of the latest run


@dataclass
class RunContext:
    homes: list
    sources: dict                      # platform -> source
    prior: PriorState
    as_of: datetime
    window_days: int = 7
    budget: Budget = field(default_factory=lambda: Budget(480))
    run_label: str = ""
    jobs: list = field(default_factory=list)
    reviews: list = field(default_factory=list)   # every review in the window this run: dicts with tags
    new_review_ids: set = field(default_factory=set)
    started: float = field(default_factory=time.monotonic)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    alerts: list = field(default_factory=list)
    sheet_url: str = ""
    tab_name: str = ""
    previous_tab: str | None = None

    def __post_init__(self):
        now = datetime.now(timezone.utc)
        self.run_label = now.strftime("%Y-%m-%d %H:%M:%S UTC")

    @property
    def cutoff(self) -> datetime:
        return self.as_of - timedelta(days=self.window_days)

    @property
    def seconds(self) -> float:
        return round(time.monotonic() - self.started, 1)


def collect_platform(ctx: RunContext, platform: str) -> list[JobResult]:
    source = ctx.sources[platform]
    homes = ctx.homes
    prev = {h.id: ctx.prior.review_counts.get((h.id, platform)) for h in homes}

    def job(home) -> JobResult:
        if ctx.budget.remaining() <= 0:
            return JobResult(home=home.id, platform=platform, status=Status.SKIPPED_BUDGET)
        return source.collect(home, ctx.cutoff, prev[home.id])

    results = []
    pool = ThreadPoolExecutor(PLATFORM_CONCURRENCY.get(platform, 1))
    try:
        futures = [(h, pool.submit(job, h)) for h in homes]
        for home, fut in futures:
            try:
                res = fut.result(timeout=min(JOB_TIMEOUT, max(1.0, ctx.budget.remaining())))
            except FutureTimeout:
                res = JobResult(home=home.id, platform=platform, status=Status.TIMEOUT,
                                error=f"job exceeded {JOB_TIMEOUT:.0f}s")
            results.append(res)
    finally:
        # Don't wait on a stuck worker: its result is already recorded as a timeout, and every request
        # it makes is itself capped by the run budget, so it can't hold the run open.
        pool.shutdown(wait=False, cancel_futures=True)

    cutoff_date = ctx.cutoff.date().isoformat()
    for res in results:
        if res.status == Status.UNCHANGED or res.status in Status.FAILED:
            # Nothing new on the site, or this run couldn't fetch: carry last run's in-window reviews forward,
            # so they stay in the window and keep their tags instead of coming back as new next run.
            for row in ctx.prior.last_window_reviews.get((res.home, platform), []):
                if row["date"] > cutoff_date:  # same calendar-day rule as the collector
                    ctx.reviews.append(dict(row))
        seen = {row["review_id"] for row in ctx.reviews}
        for r in res.reviews:
            if r.id in seen:  # paging can return a review twice when a new one shifts the offset
                continue
            seen.add(r.id)
            stored = ctx.prior.reviews.get(r.id)
            ctx.reviews.append({
                "review_id": r.id, "home": r.home, "platform": r.platform, "date": r.date,
                "rating": r.rating, "scale": r.scale, "reviewer": r.reviewer, "text": r.text,
                "sentiment": stored["sentiment"] if stored else "",
                "praise": stored["praise"] if stored else "",
                "complaints": stored["complaints"] if stored else "",
            })
            if not stored:
                ctx.new_review_ids.add(r.id)
    ctx.jobs = [j for j in ctx.jobs if j.platform != platform] + results
    return results
