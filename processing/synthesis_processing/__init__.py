"""Timer-triggered connections, overview, and sentiment synthesis job."""

from __future__ import annotations

import datetime as dt
import logging

import azure.functions as func


def main(timer: func.TimerRequest) -> None:
    """Generate connections, ticker overviews, and weekly sentiment outputs."""
    from shared.pipeline import ProcessingRunner

    if timer.past_due:
        logging.warning("Synthesis processing timer is past due.")
    logging.info("Starting synthesis processing timer at %s", dt.datetime.now(dt.UTC).isoformat())
    ProcessingRunner.from_env().run_synthesis()
