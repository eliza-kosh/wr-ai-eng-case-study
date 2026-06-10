from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "processing"))

from shared.citations import extract_cited_item_ids, find_invalid_citations
from shared.config import ProcessingConfig
from shared.models import sentiment_to_score
from shared.openai_client import parse_connection, parse_enrichment
from shared.sentiment import rolling_z_score


def test_parse_enrichment_clamps_and_normalizes() -> None:
    result = parse_enrichment(
        {
            "relevance": 99,
            "sentiment": "weird",
            "sentiment_rationale": "  unclear  ",
            "themes": ["support", "", "pricing", "enterprise", "extra"],
            "firsthand": True,
            "firsthand_type": "customer",
            "summary": "  Support response times worsened for enterprise users.  ",
        }
    )

    assert result.relevance == 10
    assert result.sentiment == "neutral"
    assert result.sentiment_rationale == "unclear"
    assert result.themes == ("support", "pricing", "enterprise", "extra")
    assert result.firsthand is True
    assert result.summary == "Support response times worsened for enterprise users."


def test_parse_connection_defaults_unknown_type() -> None:
    result = parse_connection(
        {
            "valid": True,
            "confidence": 2,
            "narrative": "Two sources point to support disruption.",
            "stock_relevance": "Could pressure enterprise retention.",
            "connection_type": "unknown",
        }
    )

    assert result.valid is True
    assert result.confidence == 1.0
    assert result.connection_type == "corroborating"


def test_sentiment_to_score() -> None:
    assert sentiment_to_score("bullish") == 1
    assert sentiment_to_score("bearish") == -1
    assert sentiment_to_score("neutral") == 0
    assert sentiment_to_score("mixed") == 0


def test_rolling_z_score() -> None:
    score = rolling_z_score(3.0, [0.0, 0.0, 1.0, 1.0])
    assert score is not None
    assert score > 3
    assert rolling_z_score(1.0, [1.0]) is None
    assert rolling_z_score(1.0, [1.0, 1.0, 1.0]) is None


def test_citation_validation() -> None:
    text = "Signal cites reddit:abc123 and github:def456 but not the missing one."
    assert extract_cited_item_ids(text) == {"reddit:abc123", "github:def456"}
    assert find_invalid_citations(text, {"reddit:abc123"}) == {"github:def456"}


def test_config_defaults_match_plan() -> None:
    config = ProcessingConfig()
    assert config.sources == ("reddit", "hacker_news", "github")
    assert config.relevance_threshold == 5
    assert config.similarity_threshold == 0.75
    assert config.connection_confidence_threshold == 0.6
    assert config.max_agent_searches == 5
    assert config.anthropic_connection_model == "claude-opus-4-8"
    assert config.anthropic_summary_model == "claude-opus-4-8"
    assert config.openai_embedding_model == "text-embedding-3-small"
