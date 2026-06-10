"""Pure sentiment helpers used by tests and processing logic."""

from __future__ import annotations

from shared.models import sentiment_to_score


def rolling_z_score(current: float, history: list[float]) -> float | None:
    """Compute a z-score against prior history, returning None when undefined."""
    if len(history) < 2:
        return None
    mean = sum(history) / len(history)
    variance = sum((value - mean) ** 2 for value in history) / (len(history) - 1)
    if variance == 0:
        return None
    return (current - mean) / (variance**0.5)


__all__ = ["rolling_z_score", "sentiment_to_score"]
