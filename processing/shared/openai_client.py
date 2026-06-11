"""Model client wrapper for OpenAI enrichment and embedding calls."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import OpenAI

from shared.config import ProcessingConfig
from shared.models import EnrichmentResult, SourceItem

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


class OpenAIProcessorClient:
    """Wrapper around OpenAI enrichment and embedding calls."""

    def __init__(
        self,
        config: ProcessingConfig,
        client: "OpenAI | None" = None,
    ) -> None:
        self.config = config
        if client is None:
            from openai import OpenAI

            client = OpenAI()
        self.client = client

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
