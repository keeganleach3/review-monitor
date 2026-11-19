"""Command line.

  python -m review_monitor run                      # replay recorded data -> review_monitor.xlsx, no API key needed
  python -m review_monitor run --mode live --sheet google           # the real thing
  python -m review_monitor run --mode live --sheet google --every 10  # on a schedule
  python -m review_monitor verify                   # two live runs into the Google Sheet + a requirements check
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from . import alerts
from .agent import describe, run_agent, run_without_model
from .collect import RunContext
from .http import Budget, Fetcher
from .models import AIRBNB, BOOKING, Home
from .sheet import GoogleSheets, Xlsx, load_prior, update_run_stats
from .sources import AirbnbSource, BookingSource
from .tools import Tools

ROOT = Path(__file__).resolve().parent.parent


def load_homes(path) -> list[Home]:
    return [Home(h["id"], h["country"], h["airbnb"], h["booking"])
            for h in tomllib.loads(Path(path).read_text(encoding="utf-8"))["home"]]


def make_backend(args):
    if args.sheet == "google":
        sheet_id = os.environ.get("SHEET_ID")
        if not sheet_id:
            sys.exit("Set SHEET_ID (and GOOGLE_APPLICATION_CREDENTIALS). See README > Run it.")
        return GoogleSheets(sheet_id)
    return Xlsx(args.xlsx)


def run_once(args) -> RunContext:
    budget = Budget(args.budget)
    fetcher = Fetcher(mode=args.mode, fixtures_dir=args.fixtures, budget=budget)
    as_of = datetime.now(timezone.utc)
    if args.mode == "replay":
        as_of = datetime.fromisoformat(json.loads((Path(args.fixtures) / "manifest.json").read_text())["as_of"])
    backend = make_backend(args)
    prior, previous_tab = load_prior(backend)
    ctx = RunContext(homes=load_homes(args.listings),
                     sources={AIRBNB: AirbnbSource(fetcher), BOOKING: BookingSource(fetcher)},
                     prior=prior, as_of=as_of, budget=budget)
    tools = Tools(ctx, backend, previous_tab)
    # Replay is for offline runs and tests: it never calls the model, even when a key is set.
    use_model = args.mode != "replay" and not args.no_llm and os.environ.get("ANTHROPIC_API_KEY")
    final = run_agent(tools) if use_model else run_without_model(tools)
    update_run_stats(ctx, backend)
    print(describe(ctx))
    print(f"  agent: {final}")
    return ctx


def cmd_run(args) -> int:
    """One run; with --every, one run every N minutes until stopped (or until --runs runs).
    Exits 1 if any run raised an alert, so a scheduler shows the failure."""
    alerted = False
    for i in itertools.count(1):
        started = time.monotonic()
        try:
            alerted |= bool(run_once(args).alerts)
        except Exception as exc:  # e.g. the Sheet is unreachable: nothing was written, so say so
            alerts.send("Review Monitor run failed", f"The run failed: {type(exc).__name__}: {exc}. "
                        "The last complete run tab is still the one the next run reads.")
            if not args.every:
                raise
            print(f"run failed: {type(exc).__name__}: {exc}")  # one bad run must not end the schedule
            alerted = True
        if not args.every or (args.runs and i >= args.runs):
            return 1 if alerted else 0
        wait = max(0.0, args.every * 60 - (time.monotonic() - started))  # fixed cadence; runs never overlap
        print(f"next run in {wait / 60:.1f} min")
        time.sleep(wait)


def cmd_verify(args) -> int:
    """Two live runs of the real agent into the Google Sheet, then check what the Sheet shows."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("verify runs the real agent: set ANTHROPIC_API_KEY (see .env.example).")
    args.mode, args.sheet, args.no_llm = "live", "google", False
    runs_ctx = []
    for i in range(2):
        runs_ctx.append(run_once(args))
        if i == 0 and args.gap:
            print(f"waiting {args.gap} min before run 2")
            time.sleep(args.gap * 60)

    first, second = runs_ctx
    sheet, n = make_backend(args), len(load_homes(args.listings))
    tabs = sheet.tab_names()
    dash = sheet.read("Dashboard", f"B8:P{7 + n}", render="FORMATTED_VALUE")
    formulas = sheet.read("Dashboard", f"C8:N{7 + n}", render="FORMULA")
    rows = [r for r in sheet.read(second.tab_name) if len(r) > 11 and r[11] and r[11] != "Review ID"]
    cutoff = second.cutoff.date().isoformat()

    def cell(row, i):
        return row[i] if len(row) > i else ""
    statuses = ("ok", "unchanged", "blocked_by_bot_protection", "no_ratings_yet")  # "ok" covers ok_*
    checks = [
        ("Dashboard, Changes and both run tabs exist",
         {"Dashboard", "Changes", first.tab_name, second.tab_name} <= set(tabs)),
        (f"Airbnb rating, review count and category scores for all {n} homes",
         len(dash) == n and all(cell(r, i) != "" for r in dash for i in (1, 2, 8, 9, 10))),
        ("Every Dashboard number is a formula; data columns read the latest run tab",  # G:I derive from C:F
         len(formulas) == n and all(str(v).startswith("=") for r in formulas for v in r) and
         all(second.tab_name in str(r[i]) for r in formulas for i in (0, 1, 2, 3, 7, 8, 9, 10, 11))),
        ("Airbnb status ok/unchanged for every home", all(str(cell(r, 13)).startswith(("ok", "unchanged")) for r in dash)),
        ("Booking.com status reported; review counts blank where blocked",
         all(str(cell(r, 14)).startswith(statuses) and (cell(r, 12) == "") == str(cell(r, 14)).startswith("blocked")
             for r in dash)),
        (f"The run tab holds exactly the {len(second.reviews)} reviews collected, each tagged and inside the window",
         bool(rows) and {r[11] for r in rows} == {r["review_id"] for r in second.reviews} and
         all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r[2])) and str(r[2]) > cutoff and r[7] for r in rows)),
        ("Model called only when there were new reviews to tag",
         all((c.input_tokens > 0) == bool(c.new_review_ids) for c in runs_ctx)),
    ]
    if args.gap:
        minutes = (second.started - first.started) / 60
        checks.append((f"Run 2 started {minutes:.1f} min after run 1", minutes >= args.gap))
    print("\nRequirements check")
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    for i, c in enumerate(runs_ctx, 1):
        print(f"  run {i}: {c.input_tokens:,} input tokens ({c.cache_read_tokens:,} from cache), {c.output_tokens:,} output")
    print(f"  sheet: {sheet.url}")
    return 0 if all(ok for _, ok in checks) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="review_monitor")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("verify", cmd_verify)):
        s = sub.add_parser(name)
        s.add_argument("--mode", choices=["replay", "live", "record"], default="replay")
        s.add_argument("--sheet", choices=["xlsx", "google"], default="xlsx")
        s.add_argument("--xlsx", default="review_monitor.xlsx")
        s.add_argument("--listings", default=str(ROOT / "listings.toml"))
        s.add_argument("--fixtures", default=str(ROOT / "fixtures"))
        s.add_argument("--budget", type=float, default=480, help="seconds per run (default 8 min)")
        s.add_argument("--no-llm", action="store_true", help="skip the model; keyword tagging")
        s.add_argument("--every", type=float, default=0, help="minutes between runs")
        s.add_argument("--runs", type=int, default=0, help="with --every: stop after N runs (default: until stopped)")
        s.add_argument("--gap", type=float, default=0, help="verify: minutes between the two runs")
        s.set_defaults(func=fn)
    args = p.parse_args(argv)
    return args.func(args)
