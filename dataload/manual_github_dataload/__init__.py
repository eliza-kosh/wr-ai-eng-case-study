"""Key-protected manual GitHub dataload diagnostic endpoint."""

from __future__ import annotations

import json
import traceback

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Run GitHub dataload and return a diagnostic response."""
    try:
        from sources.github import GitHubDataloadRunner

        GitHubDataloadRunner().run_all()
        return func.HttpResponse(
            json.dumps({"status": "success"}),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as exc:
        return func.HttpResponse(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                }
            ),
            mimetype="application/json",
            status_code=500,
        )
