"""Failure summary: one rule set, sent by email when SMTP is configured, otherwise logged."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from .models import Status


def evaluate(jobs: list, failure_threshold: float = 0.30) -> list[str]:
    """Reasons to alert. Booking's bot wall is expected and reported every run, so it doesn't count
    as a failure on its own; errors, timeouts and budget overruns do."""
    if not jobs:
        return ["run produced no jobs"]
    reasons = []
    # Per platform: each site is its own pipeline with its own on-call fix, so one site's healthy homes
    # must not dilute another's outage. Expected blocks count as reported, not failed.
    for platform in sorted({j.platform for j in jobs}):
        pj = [j for j in jobs if j.platform == platform]
        failed = sum(j.status in Status.FAILED - Status.EXPECTED_BLOCK for j in pj)
        if failed / len(pj) > failure_threshold:
            reasons.append(f"{platform}: {failed}/{len(pj)} jobs failed (threshold {failure_threshold:.0%})")
    return reasons


def send(subject: str, body: str, log_path: str = "alerts.log") -> str:
    to, host = os.environ.get("ALERT_EMAIL_TO"), os.environ.get("ALERT_SMTP_HOST")
    if to and host:
        msg = EmailMessage()
        msg["Subject"], msg["To"] = subject, to
        msg["From"] = os.environ.get("ALERT_EMAIL_FROM") or to
        msg.set_content(body)
        try:
            with smtplib.SMTP(host, int(os.environ.get("ALERT_SMTP_PORT", "587")), timeout=30) as s:
                s.starttls()
                if os.environ.get("ALERT_SMTP_USER"):
                    s.login(os.environ["ALERT_SMTP_USER"], os.environ["ALERT_SMTP_PASSWORD"])
                s.send_message(msg)
            return f"email to {to}"
        except (OSError, smtplib.SMTPException) as exc:  # a failed email must not lose the alert
            body += f"\n\n(email to {to} failed: {exc})"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{subject}\n{body}\n\n")
    return f"logged to {log_path}"
