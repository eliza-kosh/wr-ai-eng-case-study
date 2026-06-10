"""Timer-triggered Hacker News dataload function."""

from __future__ import annotations

import datetime as dt
import logging

import azure.functions as func


def main(timer: func.TimerRequest) -> None:
    """Load Hacker News source data into Blob Storage and PostgreSQL."""
    from sources.hacker_news import HackerNewsDataloadRunner

    if timer.past_due:
        logging.warning("Hacker News dataload timer is past due.")
    logging.info(
        "Starting Hacker News dataload timer at %s",
        dt.datetime.now(dt.UTC).isoformat(),
    )
    HackerNewsDataloadRunner().run_all()
