"""Timer-triggered Reddit dataload function."""

from __future__ import annotations

import datetime as dt
import logging

import azure.functions as func


def main(timer: func.TimerRequest) -> None:
    """Load Reddit source data into Blob Storage and PostgreSQL."""
    from sources.reddit import RedditDataloadRunner

    if timer.past_due:
        logging.warning("Reddit dataload timer is past due.")
    logging.info("Starting Reddit dataload timer at %s", dt.datetime.now(dt.UTC).isoformat())
    RedditDataloadRunner().run_all()
