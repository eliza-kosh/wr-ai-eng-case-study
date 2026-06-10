"""Timer-triggered GitHub dataload function."""

from __future__ import annotations

import datetime as dt
import logging

import azure.functions as func

from sources.github import GitHubDataloadRunner


def main(timer: func.TimerRequest) -> None:
    """Load GitHub source data into Blob Storage and PostgreSQL."""
    if timer.past_due:
        logging.warning("GitHub dataload timer is past due.")
    logging.info("Starting GitHub dataload timer at %s", dt.datetime.now(dt.UTC).isoformat())
    GitHubDataloadRunner().run_all()
