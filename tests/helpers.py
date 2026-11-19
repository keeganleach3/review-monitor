import argparse
from datetime import datetime, timedelta
from pathlib import Path

from review_monitor.cli import load_homes
from review_monitor.collect import PriorState, RunContext
from review_monitor.http import Budget, Fetcher
from review_monitor.models import AIRBNB, BOOKING
from review_monitor.sheet import Xlsx
from review_monitor.sources import AirbnbSource, BookingSource
from review_monitor.tools import Tools

ROOT = Path(__file__).resolve().parent.parent
HOMES = load_homes(ROOT / "listings.toml")
AS_OF = datetime.fromisoformat("2026-11-18T21:20:00+00:00")
CUTOFF = AS_OF - timedelta(days=7)


def run_args(fixtures, xlsx, **kw):
    base = dict(mode="replay", sheet="xlsx", xlsx=str(xlsx), listings=str(ROOT / "listings.toml"),
                fixtures=str(fixtures), budget=60.0, no_llm=True, every=0, runs=1, gap=0)
    base.update(kw)
    return argparse.Namespace(**base)


def make_tools(tmp_path, fixtures, prior=None, previous_tab=None):
    budget = Budget(60)
    f = Fetcher("replay", fixtures, budget=budget)
    ctx = RunContext(homes=HOMES, sources={AIRBNB: AirbnbSource(f), BOOKING: BookingSource(f)},
                     prior=prior or PriorState(), as_of=AS_OF, budget=budget)
    return Tools(ctx, Xlsx(tmp_path / "out.xlsx"), previous_tab)
