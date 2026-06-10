"""Azure Functions entrypoint for scheduled source dataloads."""

from __future__ import annotations

import datetime as dt
import logging
import os

import azure.functions as func

from sources.github import GitHubDataloadRunner
from sources.hacker_news import HackerNewsDataloadRunner
from sources.reddit import RedditDataloadRunner

app = func.FunctionApp()


@app.timer_trigger(
    schedule=os.getenv("REDDIT_DATALOAD_SCHEDULE", "0 0 * * * *"),
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def reddit_dataload(timer: func.TimerRequest) -> None:
    """Load Reddit data into Blob Storage and PostgreSQL."""
    if timer.past_due:
        logging.warning("Reddit dataload timer is past due.")
    logging.info("Starting Reddit dataload timer at %s", dt.datetime.now(dt.UTC).isoformat())
    RedditDataloadRunner().run_all()


@app.timer_trigger(
    schedule=os.getenv("HACKER_NEWS_DATALOAD_SCHEDULE", "0 10 * * * *"),
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def hacker_news_dataload(timer: func.TimerRequest) -> None:
    """Load Hacker News data into Blob Storage and PostgreSQL."""
    if timer.past_due:
        logging.warning("Hacker News dataload timer is past due.")
    logging.info(
        "Starting Hacker News dataload timer at %s",
        dt.datetime.now(dt.UTC).isoformat(),
    )
    HackerNewsDataloadRunner().run_all()


@app.timer_trigger(
    schedule=os.getenv("GITHUB_DATALOAD_SCHEDULE", "0 20 * * * *"),
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def github_dataload(timer: func.TimerRequest) -> None:
    """Load GitHub data into Blob Storage and PostgreSQL."""
    if timer.past_due:
        logging.warning("GitHub dataload timer is past due.")
    logging.info("Starting GitHub dataload timer at %s", dt.datetime.now(dt.UTC).isoformat())
    GitHubDataloadRunner().run_all()
