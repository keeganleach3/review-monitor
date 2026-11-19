"""Plain data types shared by every stage of the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field

AIRBNB = "Airbnb"
BOOKING = "Booking.com"
SCALE = {AIRBNB: 5, BOOKING: 10}  # each site keeps its own scale; we never rescale raw data


class Status:
    """Outcome of one (home, platform) job. Stored as plain strings."""
    OK = "ok"                                  # metrics + reviews collected
    OK_NO_RECENT = "ok_no_recent_reviews"      # worked; nothing new in the window
    METRICS_ONLY = "ok_metrics_only"           # rating read; this source can't see reviews
    UNCHANGED = "unchanged"                    # review count same as last run -> review fetch skipped
    BLOCKED = "blocked_by_bot_protection"      # site served a bot challenge; we stop, by design
    NO_RATINGS = "no_ratings_yet"
    TIMEOUT = "timeout"
    ERROR = "error"
    SKIPPED_BUDGET = "skipped_run_budget"      # run-level time budget ran out before this job started

    FAILED = {BLOCKED, TIMEOUT, ERROR, SKIPPED_BUDGET}
    HEALTHY = {OK, OK_NO_RECENT, METRICS_ONLY, UNCHANGED, NO_RATINGS}
    EXPECTED_BLOCK = {BLOCKED}  # Booking's bot wall: reported every run, alerted only if something else breaks


@dataclass(frozen=True)
class Home:
    id: int
    country: str
    airbnb: str
    booking: str


@dataclass
class Metric:
    name: str
    value: float


@dataclass
class Review:
    id: str
    home: int
    platform: str
    date: str          # YYYY-MM-DD
    rating: float | None
    reviewer: str
    text: str

    @property
    def scale(self) -> int:
        return SCALE[self.platform]


@dataclass
class JobResult:
    home: int
    platform: str
    status: str = Status.OK
    metrics: list[Metric] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0
    requests: int = 0
