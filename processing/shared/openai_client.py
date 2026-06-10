"""Model client wrapper for OpenAI preparation and Anthropic synthesis calls."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anthropic import Anthropic
    from openai import OpenAI

from shared.config import ProcessingConfig
from shared.models import ConnectionCandidate, ConnectionVerification, EnrichmentResult, SourceItem

ENRICHMENT_SCHEMA: dict[str, Any] = {
    "name": "item_enrichment",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "relevance": {"type": "integer", "minimum": 0, "maximum": 10},
            "sentiment": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
            "sentiment_rationale": {"type": "string"},
            "themes": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
            "firsthand": {"type": "boolean"},
            "firsthand_type": {"type": ["string", "null"]},
            "summary": {"type": "string"},
        },
        "required": [
            "relevance",
            "sentiment",
            "sentiment_rationale",
            "themes",
            "firsthand",
            "firsthand_type",
            "summary",
        ],
    },
    "strict": True,
}

CONNECTION_SCHEMA: dict[str, Any] = {
    "name": "connection_verification",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "valid": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "narrative": {"type": "string"},
            "stock_relevance": {"type": "string"},
            "connection_type": {
                "type": "string",
                "enum": ["causal", "corroborating", "contradicting", "leading_indicator"],
            },
        },
        "required": ["valid", "confidence", "narrative", "stock_relevance", "connection_type"],
    },
    "strict": True,
}

SUMMARY_SCHEMA: dict[str, Any] = {
    "name": "brain_summary",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string"},
            "key_signals": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
            "cross_source_connections": {"type": "array", "items": {"type": "string"}},
            "bear_case": {"type": "string"},
            "confidence": {"type": "string"},
            "cited_item_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "headline",
            "key_signals",
            "cross_source_connections",
            "bear_case",
            "confidence",
            "cited_item_ids",
        ],
    },
    "strict": True,
}


class OpenAIProcessorClient:
    """Wrapper around OpenAI prep calls and Anthropic synthesis calls."""

    def __init__(
        self,
        config: ProcessingConfig,
        client: "OpenAI | None" = None,
        anthropic_client: "Anthropic | None" = None,
    ) -> None:
        self.config = config
        if client is None:
            from openai import OpenAI

            client = OpenAI()
        self.client = client
        self.anthropic_client = anthropic_client

    def enrich_item(self, item: SourceItem) -> EnrichmentResult:
        """Extract structured analyst metadata for one source item."""
        prompt = _enrichment_prompt(item)
        payload = self._json_response(
            model=self.config.openai_enrichment_model,
            system=(
                "You are an equity analyst assistant. Extract only investment-relevant "
                "metadata from alternative-data source items. Sentiment is directional "
                "for the stock, not general positivity."
            ),
            user=prompt,
            schema=ENRICHMENT_SCHEMA,
        )
        return parse_enrichment(payload)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed normalized summaries."""
        if not texts:
            return []
        response = self.client.embeddings.create(
            model=self.config.openai_embedding_model,
            input=texts,
            dimensions=self.config.embedding_dimensions,
        )
        return [list(item.embedding) for item in response.data]

    def verify_connection(self, candidate: ConnectionCandidate) -> ConnectionVerification:
        """Ask the model whether a similarity candidate is a meaningful connection."""
        payload = self._anthropic_json_response(
            model=self.config.anthropic_connection_model,
            system=(
                "You verify cross-source investment signals. Reject surface keyword overlap, "
                "generic negativity, structural similarity, and obvious public-news repetition."
            ),
            user=_connection_prompt(candidate),
            schema=CONNECTION_SCHEMA,
        )
        return parse_connection(payload)

    def generate_summary(self, ticker: str, context: dict[str, Any]) -> dict[str, Any]:
        """Generate a ticker-level brain summary from collected context."""
        payload = self._anthropic_json_response(
            model=self.config.anthropic_summary_model,
            system=(
                "You write concise alt-data briefs for public-equity analysts. Cite source_item_id "
                "values exactly when making evidence-backed claims."
            ),
            user=json.dumps({"ticker": ticker, "context": context}, default=str),
            schema=SUMMARY_SCHEMA,
        )
        return payload

    def propose_search(self, ticker: str, context: dict[str, Any], searches_used: int) -> str | None:
        """Ask whether one more semantic search is useful; return query or None."""
        payload = self._anthropic_json_response(
            model=self.config.anthropic_summary_model,
            system=(
                "Decide whether another semantic search would materially improve an analyst brief. "
                "Return null when existing evidence is enough or searches are diminishing."
            ),
            user=json.dumps(
                {
                    "ticker": ticker,
                    "searches_used": searches_used,
                    "searches_remaining": self.config.max_agent_searches - searches_used,
                    "context": context,
                },
                default=str,
            ),
            schema={
                "name": "search_decision",
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"query": {"type": ["string", "null"]}},
                    "required": ["query"],
                },
                "strict": True,
            },
        )
        query = payload.get("query")
        return query.strip() if isinstance(query, str) and query.strip() else None



    def _anthropic_json_response(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        client = self._anthropic_client()
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=(
                f"{system}\n\nReturn only valid JSON matching this JSON Schema. "
                f"Do not include markdown fences or commentary. Schema: {json.dumps(schema['schema'])}"
            ),
            messages=[{"role": "user", "content": user}],
        )
        text = _anthropic_response_text(response)
        text = _strip_json_fences(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logging.error("Anthropic response was not valid JSON: %s", text[:500])
            raise

    def _anthropic_client(self) -> "Anthropic":
        if self.anthropic_client is None:
            from anthropic import Anthropic

            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is required for synthesis processing")
            self.anthropic_client = Anthropic(api_key=api_key)
        return self.anthropic_client

    def _json_response(self, model: str, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = self.client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text={"format": {"type": "json_schema", **schema}},
        )
        text = _response_text(response)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logging.error("OpenAI response was not valid JSON: %s", text[:500])
            raise


def parse_enrichment(payload: dict[str, Any]) -> EnrichmentResult:
    """Parse and normalize enrichment JSON."""
    sentiment = str(payload.get("sentiment", "neutral")).lower()
    if sentiment not in {"bullish", "bearish", "neutral"}:
        sentiment = "neutral"
    relevance = max(0, min(10, int(payload.get("relevance", 0))))
    themes = tuple(str(theme).strip() for theme in payload.get("themes", []) if str(theme).strip())
    return EnrichmentResult(
        relevance=relevance,
        sentiment=sentiment,  # type: ignore[arg-type]
        sentiment_rationale=str(payload.get("sentiment_rationale", "")).strip(),
        themes=themes[:4],
        firsthand=bool(payload.get("firsthand", False)),
        firsthand_type=payload.get("firsthand_type"),
        summary=str(payload.get("summary", "")).strip(),
    )


def parse_connection(payload: dict[str, Any]) -> ConnectionVerification:
    """Parse and normalize connection verification JSON."""
    connection_type = str(payload.get("connection_type", "corroborating"))
    if connection_type not in {"causal", "corroborating", "contradicting", "leading_indicator"}:
        connection_type = "corroborating"
    return ConnectionVerification(
        valid=bool(payload.get("valid", False)),
        confidence=max(0.0, min(1.0, float(payload.get("confidence", 0.0)))),
        narrative=str(payload.get("narrative", "")).strip(),
        stock_relevance=str(payload.get("stock_relevance", "")).strip(),
        connection_type=connection_type,  # type: ignore[arg-type]
    )


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences that some models wrap around JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[^\n]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _anthropic_response_text(response: Any) -> str:
    chunks: list[str] = []
    for content in getattr(response, "content", []) or []:
        text = getattr(content, "text", None)
        if text:
            chunks.append(text)
    return "".join(chunks)


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text
    chunks: list[str] = []
    for output in getattr(response, "output", []) or []:
        for content in getattr(output, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(text)
    return "".join(chunks)


def _enrichment_prompt(item: SourceItem) -> str:
    source_guidance = {
        "github": "A spike in bug reports is bearish; feature work or external contribution can be bullish.",
        "reddit": "Separate firsthand customer/developer experience from speculation, memes, or trading chatter.",
        "hacker_news": "Prioritize technical buyer, developer, operator, and competitor-switching evidence.",
    }.get(item.source, "Prioritize concrete firsthand or operational evidence.")
    return json.dumps(
        {
            "source_guidance": source_guidance,
            "source_item_id": item.source_item_id,
            "ticker": item.ticker,
            "source": item.source,
            "title": item.title,
            "body": item.body,
            "published_at": item.published_at,
            "metadata": item.metadata,
        },
        default=str,
    )


def _connection_prompt(candidate: ConnectionCandidate) -> str:
    return json.dumps(
        {
            "ticker": candidate.ticker,
            "similarity": candidate.similarity,
            "item_a": {
                "source_item_id": candidate.item_a_id,
                "source": candidate.source_a,
                "published_at": candidate.published_a,
                "summary": candidate.summary_a,
            },
            "item_b": {
                "source_item_id": candidate.item_b_id,
                "source": candidate.source_b,
                "published_at": candidate.published_b,
                "summary": candidate.summary_b,
            },
        },
        default=str,
    )
