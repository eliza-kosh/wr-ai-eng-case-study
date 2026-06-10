"""Timer-triggered enrichment and embedding preparation job."""

from __future__ import annotations

import datetime as dt
import logging

import azure.functions as func


def main(timer: func.TimerRequest) -> None:
    """Enrich new source items and embed relevant normalized summaries."""
    from shared.pipeline import ProcessingRunner

    if timer.past_due:
        logging.warning("Prepare processing timer is past due.")
    logging.info("Starting prepare processing timer at %s", dt.datetime.now(dt.UTC).isoformat())
    ProcessingRunner.from_env().run_prepare()
