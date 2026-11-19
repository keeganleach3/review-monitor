"""The run's steps, as methods on one object. Code calls the collectors and the Sheet writer; the model
gets only the two tagging tools in TOOL_SPECS. Each returns a short JSON summary, so the only data the model
ever reads is the text of reviews it hasn't tagged before."""
from __future__ import annotations

import json
from collections import Counter

from . import alerts
from .collect import RunContext, collect_platform
from .models import AIRBNB, BOOKING, Status
from .sheet import publish
from .tagging import TAXONOMY, THEMES

NEW_REVIEW_PAGE = 30

TOOL_SPECS = [  # the model's only tools
    {"name": "get_new_reviews",
     "description": f"Return up to {NEW_REVIEW_PAGE} reviews collected this run that have never been tagged (id, rating, "
                    "out_of, text). Reviews tagged on earlier runs keep their tags and are not returned.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "save_review_tags",
     "description": "Save sentiment and themes for reviews returned by get_new_reviews. Use only the allowed themes; "
                    "a theme can't be both praise and complaint for one review.",
     "input_schema": {"type": "object", "required": ["tags"], "properties": {"tags": {"type": "array", "items": {
         "type": "object", "required": ["id", "sentiment", "praise", "complaints"], "properties": {
             "id": {"type": "string"},
             "sentiment": {"type": "string", "enum": ["positive", "mixed", "negative"]},
             "praise": {"type": "array", "items": {"type": "string", "enum": THEMES}},
             "complaints": {"type": "array", "items": {"type": "string", "enum": THEMES}}}}}}}},
]


class Tools:
    def __init__(self, ctx: RunContext, backend, previous_tab: str | None):
        self.ctx, self.backend, self.previous_tab = ctx, backend, previous_tab
        self.written: dict | None = None      # write_run_to_sheet's result, once the run is written
        self.alert_sent = False
        self.collected: dict[str, dict] = {}  # platform -> its collection summary

    # -- tools ----------------------------------------------------------------------------------
    def _collect(self, platform: str) -> dict:
        if platform in self.collected:  # once per run: a second call would refetch and duplicate rows
            return {**self.collected[platform], "note": "Already collected this run; not repeated."}
        results = collect_platform(self.ctx, platform)
        counts = Counter(r.status for r in results)
        failed = [f"home {r.home}: {r.error or r.status}" for r in results if r.status in Status.FAILED]
        out = {"platform": platform, "homes": len(results), "statuses": dict(counts),
               "reviews_in_window": sum(1 for r in self.ctx.reviews if r["platform"] == platform),
               "new_reviews": sum(1 for r in self.ctx.reviews if r["platform"] == platform and r["review_id"] in self.ctx.new_review_ids)}
        if failed:
            out["failed"] = failed[:3] + ([f"+{len(failed) - 3} more"] if len(failed) > 3 else [])
        if counts.get(Status.BLOCKED) == len(results):
            out["note"] = "Bot challenge: expected for Booking.com. Reported; not retried."
        self.collected[platform] = out
        return out

    def collect_airbnb(self) -> dict:
        return self._collect(AIRBNB)

    def collect_booking(self) -> dict:
        return self._collect(BOOKING)

    def get_new_reviews(self) -> dict:
        pending = [r for r in self.ctx.reviews if r["review_id"] in self.ctx.new_review_ids and not r["sentiment"]]
        page = pending[:NEW_REVIEW_PAGE]
        return {"reviews": [{"id": r["review_id"], "rating": r["rating"], "out_of": r["scale"], "text": r["text"]} for r in page],
                "remaining_after_this": len(pending) - len(page)}

    def save_review_tags(self, tags: list) -> dict:
        # Only this run's new reviews can be tagged; tags from earlier runs are kept as they are.
        by_id: dict[str, list] = {}
        for r in self.ctx.reviews:
            if r["review_id"] in self.ctx.new_review_ids:
                by_id.setdefault(r["review_id"], []).append(r)
        saved, rejected = 0, []
        for t in tags:
            rows = by_id.get(t.get("id"))
            if not rows or t.get("sentiment") not in {"positive", "mixed", "negative"}:
                rejected.append(t.get("id"))
                continue
            complaints = sorted({x for x in t.get("complaints", []) if x in TAXONOMY})
            praise = sorted({x for x in t.get("praise", []) if x in TAXONOMY} - set(complaints))
            for row in rows:
                row["sentiment"] = t["sentiment"]
                row["praise"], row["complaints"] = ", ".join(praise), ", ".join(complaints)
            saved += 1
        return {"saved": saved, "rejected_ids": rejected}

    def write_run_to_sheet(self) -> dict:
        if self.written:  # once per run: a second call would add a second run tab
            return {**self.written, "note": "Already written this run; not repeated."}
        missing = [p for p in (AIRBNB, BOOKING) if p not in self.collected]
        untagged = len(self.get_new_reviews()["reviews"])
        if missing or untagged:  # the run tab becomes the next run's memory, so it must be complete
            return {"error": "Not written yet: " + "; ".join(
                [f"collect {p} first" for p in missing] + ([f"{untagged}+ new reviews still untagged"] if untagged else []))}
        self.ctx.alerts = alerts.evaluate(self.ctx.jobs)
        tab = publish(self.ctx, self.backend, self.previous_tab)
        self.written = {"tab": tab, "alert_required": bool(self.ctx.alerts), "alert_reasons": self.ctx.alerts}
        return self.written

    def send_failure_summary(self, summary: str) -> dict:
        if not self.ctx.alerts:
            return {"sent": False, "reason": "failure threshold not met; nothing sent"}
        if self.alert_sent:
            return {"sent": False, "reason": "already sent for this run"}
        channel = alerts.send(f"Review Monitor run {self.ctx.run_label}: needs attention",
                              summary + "\n\n" + "\n".join(f"- {a}" for a in self.ctx.alerts) + f"\n\nSheet: {self.ctx.sheet_url}")
        self.alert_sent = True
        return {"sent": True, "via": channel}

    # -- dispatch -------------------------------------------------------------------------------
    def call(self, name: str, args: dict) -> str:
        fn = getattr(self, name, None)
        if name not in {s["name"] for s in TOOL_SPECS} or fn is None:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            return json.dumps(fn(**(args or {})), ensure_ascii=False)
        except Exception as exc:  # report to the model instead of crashing the run
            return json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:300]}"})
