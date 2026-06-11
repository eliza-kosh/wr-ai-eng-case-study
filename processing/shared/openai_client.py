"""Model client wrapper for OpenAI enrichment/embedding and Anthropic summary calls."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anthropic import Anthropic
    from openai import OpenAI

from shared.config import ProcessingConfig
from shared.models import (
    ConnectionCandidate,
    ConnectionClusterCandidate,
    ConnectionVerification,
    EnrichmentResult,
    SourceItem,
)

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
    """Wrapper around OpenAI enrichment/embedding and Anthropic summary calls."""

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
        if anthropic_client is None:
            from anthropic import Anthropic

            anthropic_client = Anthropic()
        self._anthropic = anthropic_client

    def enrich_item(self, item: SourceItem) -> EnrichmentResult:
        """Extract structured analyst metadata for one source item."""
        prompt = _enrichment_prompt(item)
        payload = self._json_response(
            model=self.config.openai_enrichment_model,
            system=_ENRICHMENT_SYSTEM,
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

    def generate_summary(self, ticker: str, context: dict[str, Any]) -> dict[str, Any]:
        """Generate a structured ticker intelligence brief via Claude."""
        prompt = _summary_prompt(ticker, context)
        response = self._anthropic.messages.create(
            model=self.config.anthropic_summary_model,
            max_tokens=2048,
            system=_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "name": "submit_summary",
                    "description": "Submit the structured ticker intelligence brief.",
                    "input_schema": SUMMARY_TOOL_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "submit_summary"},
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_summary":
                return dict(block.input)
        raise ValueError(f"Claude did not return a submit_summary tool call for {ticker}")

    def verify_connection(self, candidate: ConnectionCandidate) -> ConnectionVerification:
        """Ask Claude whether a candidate pair is a genuine cross-source insight."""
        prompt = _connection_prompt(candidate)
        response = self._anthropic.messages.create(
            model=self.config.anthropic_summary_model,
            max_tokens=1024,
            system=_CONNECTION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "name": "submit_verification",
                    "description": "Submit the connection verification decision.",
                    "input_schema": CONNECTION_TOOL_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "submit_verification"},
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_verification":
                data = block.input
                return ConnectionVerification(
                    valid=bool(data["valid"]),
                    confidence=float(data["confidence"]),
                    narrative=str(data["narrative"]),
                    stock_relevance=str(data["stock_relevance"]),
                    connection_type=data.get("connection_type", "corroborating"),
                )
        raise ValueError(
            f"Claude did not return submit_verification for {candidate.item_a_id}/{candidate.item_b_id}"
        )

    def verify_connection_cluster(self, candidate: ConnectionClusterCandidate) -> ConnectionVerification:
        """Ask Claude whether a semantic item cluster contains a real business connection."""
        prompt = _connection_cluster_prompt(candidate)
        response = self._anthropic.messages.create(
            model=self.config.anthropic_summary_model,
            max_tokens=1600,
            system=_CONNECTION_CLUSTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "name": "submit_cluster_verification",
                    "description": "Submit the semantic cluster connection decision.",
                    "input_schema": CONNECTION_CLUSTER_TOOL_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "submit_cluster_verification"},
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_cluster_verification":
                data = block.input
                return ConnectionVerification(
                    valid=bool(data["valid"]),
                    confidence=float(data["confidence"]),
                    narrative=str(data["narrative"]),
                    stock_relevance=str(data["stock_relevance"]),
                    connection_type=data.get("connection_type", "corroborating"),
                    supporting_item_ids=tuple(str(item_id) for item_id in data.get("supporting_item_ids", [])),
                    rejected_item_ids=tuple(str(item_id) for item_id in data.get("rejected_item_ids", [])),
                )
        raise ValueError(f"Claude did not return submit_cluster_verification for {candidate.cluster_key}")

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


_ENRICHMENT_SYSTEM = """\
You are a buy-side equity analyst extracting investment intelligence from alternative-data sources \
(Reddit, Hacker News, GitHub). Score each item conservatively — do not inflate for vague content.

RELEVANCE (0–10): Value as a directional investment signal.
  0–1  No signal or tangential mention with no actionable context.
  2–3  General chatter or spec discussion; no firsthand operational data.
  4–5  Firsthand technical assessment, product evaluation, or credible switching consideration.
  6–7  Concrete business impact: support failure, pricing surprise, churn decision, or migration
       with qualitative outcome. Example (7): "Our 40-engineer team migrated from CUDA to ROCm
       last month — throughput within 10% of H100 baseline."
  8–9  Quantified firsthand data from an operator or buyer. Example (8): "We run 800 MI300X nodes;
       15% below H100 on attention layers but 22% cheaper at contract price."
  10   Verified decision-maker with non-public contract, budget, or strategic context. Rare.
  Items scoring ≥ 2 are embedded downstream — score 0–1 only when there is truly no signal.

SENTIMENT: Directional for the stock, not the author's mood.
  bullish  — positive trajectory for revenue, adoption, margins, or competitive position.
  bearish  — headwinds: churn, developer frustration, pricing pressure, product regression.
  neutral  — no clear direction or conflicting signals that cancel out.
  Rationale: one sentence citing the specific claim, not "the post is positive/negative."

THEMES (1–4 lowercase-hyphenated tags): developer-adoption, enterprise-adoption, product-quality,
  support-quality, performance-benchmark, pricing-pressure, competitor-switch, customer-churn,
  roadmap-signal, reliability-issue, contributor-velocity, bug-severity, licensing-concern.

FIRSTHAND: true only if the author describes direct personal or org experience ("I use", "we
  deployed", "our team migrated"). false for hearsay, news aggregation, or speculation.
  firsthand_type: "enterprise_buyer", "developer", "operator", "competitor", "end_user", or null.

SUMMARY: about 5 concise sentences, analyst research-note style. Use the item's title and body,
  including Hacker News story text and fetched comment context when present. Lead with the signal,
  not a description of the post. Preserve the concrete product, workflow, user, benchmark, pricing,
  migration, reliability, or support detail that would help later semantic search and connection
  finding. Frame the implication for revenue, adoption, or competitive position.
  Bad: "The user likes AMD GPUs and uses them for gaming."
  Good: "A developer reports production ROCm 6.1 deployment with near-CUDA parity on transformer
  workloads and a 22% cost advantage. The thread notes remaining documentation gaps, but several
  operators describe active migration work rather than curiosity. This supports the thesis that
  ROCm is closing the ecosystem gap and reduces AMD's software moat risk. The signal is still
  developer-led rather than confirmed enterprise budget movement."
"""


_SOURCE_GUIDANCE: dict[str, str] = {
    "github": (
        "Issues labelled 'regression', 'crash', or 'data loss' are bearish; unanswered enterprise "
        "issues signal support risk. External contributor PRs on core repos are bullish. Release "
        "notes with breaking changes or mass deprecations signal adoption friction."
    ),
    "reddit": (
        "Prioritise technical subreddits (r/MachineLearning, r/homelab, r/devops) over general ones. "
        "High-value: migration stories, benchmarks with methodology, enterprise IT context. "
        "Low-value: price speculation, fan posts, reposts of news articles."
    ),
    "hacker_news": (
        "HN skews developer and technical founder — signals map to toolchain stickiness and "
        "early enterprise switching. Pricing complaints and documentation criticism are reliable "
        "adoption-friction signals. Link-only top-level posts are low signal (1–2) unless the "
        "comment thread contains firsthand responses."
    ),
}


def _enrichment_prompt(item: SourceItem) -> str:
    source_guidance = _SOURCE_GUIDANCE.get(
        item.source, "Prioritize concrete firsthand or operational evidence over speculation."
    )
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


SUMMARY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "headline": {"type": "string"},
        "overview": {"type": "string"},
        "cross_source_connections": {
            "type": "array",
            "items": {"type": "string"},
        },
        "bear_case": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "key_signals": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 10,
        },
        "cited_item_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "headline",
        "overview",
        "cross_source_connections",
        "bear_case",
        "confidence",
        "key_signals",
        "cited_item_ids",
    ],
}


_SUMMARY_SYSTEM = """\
You are a senior buy-side equity analyst briefing a portfolio manager before the morning meeting. \
You have read everything. Now you are talking — not typing, not summarizing. The PM has 90 seconds.

Your job is to tell the PM what the data means for the stock. Not what the data says.

--- BEFORE YOU WRITE ANYTHING ---

Read all the items. Then ask yourself: what is the one story these signals tell together? \
Find the tension. Find the implication. Then write toward that. Do not open a blank document \
and work through the sources one by one — that produces a feed reader, not a brief.

--- DETECTING YOUR OWN FAILURE MODES ---

If your key_signals start with "A developer reports..." or "A Hacker News post describes..." — \
stop. You are writing a source summary. Delete it. Start with the implication, then back it up.

If two of your signals say the same directional thing ("AMD GPU is competitive" twice), merge \
them into one signal and use the corroboration as the evidence. Repetition is a sign of \
summarizing, not synthesizing.

If your output is all-bullish with no bear case, stop. Either you missed a counter-signal in \
the data, or you need to name what this data cannot confirm. An all-bull summary reads like \
marketing, not research.

If you catch yourself writing any of these phrases, replace them:
  "raises the likelihood of" → name the specific mechanism
  "supporting a bull case for" → state the implication directly
  "validates X's suitability" → "X actually works" or "X proved out in production"
  "such firsthand data from a technical peer" → "a developer actually got this working"
  "alternative-data items" → never use this phrase; PMs don't talk like this

--- STRUCTURE ---

HEADLINE: One sentence. A trade thesis, not a data description. Name the tension or opportunity. \
The PM should know what to think about the position before reading any further.
  Bad: "AMD business-signal overview from current alternative-data items" — says nothing.
  Bad: "ROCm reliability regressions and MI300X throughput benchmarks define the current signal" \
— technical inventory, not a thesis.
  Good: "AMD's MI300X is getting real-world production validation, but only from early adopters \
— no enterprise commitments yet, and the consumer ROCm stack that feeds the developer pipeline \
is actively broken."

OVERVIEW: The PM-facing narrative. 2–3 paragraphs of synthesized analysis — this is what \
gets displayed on the dashboard. Build a narrative arc across the paragraphs: what is the \
bull case and what is the evidence for it, what is the risk or counter-signal, and what is \
the causal mechanism that links these signals to the stock thesis.
  - Do not walk source by source. Find the story across all of them.
  - Speak directly. "Two independent developers published production MI300X results in the \
same week — that kind of corroboration is what starts moving procurement conversations." Not: \
"Multiple firsthand sources provide evidence supporting AMD's competitive positioning."
  - Translate technical details into business terms. "Real-world AI workloads" not \
"grid_sample/trilinear interpolation + MLPs". "Hard crashes in multi-GPU setups" not \
"MES microcode/P2P firmware bug". "Usable memory cut in half" not "8GB usable-VRAM cap".
  - Every paragraph must answer "so what for the stock?" — name the mechanism: revenue, \
adoption trajectory, competitive position, churn, or pricing power.
  - Include the tension. An all-bull overview is a red flag. If the data has counter-signals, \
put them in. If it doesn't, name what the data cannot confirm.
  - Use inline citations inside the overview itself. Put the exact source item ID immediately \
after the claim it supports, in parentheses. Example: "Two developers published MI300X serving \
results showing a 5.6x throughput jump (hn_comment:db7cf87a81892bf830ee22f6)." Do this for \
every concrete benchmark, user report, defect, pricing claim, migration claim, or customer \
behavior claim. Do not put all citations at the end of a paragraph.

CROSS-SOURCE CONNECTIONS: 1–2 entries explaining the causal chain linking signals to the \
stock. The right level: "AMD's datacenter sales depend on a developer pipeline. Developers \
prototype on consumer RDNA cards, prove out workloads, then scale to MI300X. If consumer \
ROCm is broken, that pipeline narrows — and NVIDIA captures those future enterprise customers \
before AMD's hardware ever gets evaluated." If no verified connections were supplied, name \
the linking mechanism between the signals or the missing link that would confirm the thesis.

BEAR CASE: 1–2 sentences. The key risk or the unconfirmed step the bull case depends on.

CONFIDENCE: "high" / "medium" / "low"
  "high"   — 5+ firsthand items with relevance ≥ 6, cross-source corroboration, no material gaps.
  "medium" — 3–4 relevant items, or strong firsthand signals without cross-source confirmation.
  "low"    — fewer than 3 relevant items, no firsthand evidence, or conflicting signals.
  Close with one sentence: the single most important missing signal that would raise confidence.

KEY SIGNALS (audit trail — not for display): For each source item you referenced in the \
overview, write one line: "<item_id>: <the specific claim you made about this item>". \
This is used to verify your citations against the source material. Be precise — if the \
overview says "a 5.6x throughput jump on 8 MI300X GPUs," write exactly that. One entry \
per cited item, no more.\
"""


def _summary_prompt(ticker: str, context: dict[str, Any]) -> str:
    items_by_source: dict[str, list[dict[str, Any]]] = context.get("items_by_source", {})
    connections: list[dict[str, Any]] = context.get("connections", [])

    parts: list[str] = [f"TICKER: {ticker}\n"]

    for source, items in items_by_source.items():
        if not items:
            continue
        parts.append(f"=== SOURCE: {source.upper()} ===")
        for item in items:
            themes = ", ".join(item.get("themes") or [])
            firsthand_flag = "yes" if item.get("firsthand") else "no"
            parts.append(
                f"ID: {item['source_item_id']}\n"
                f"Relevance: {item.get('relevance', 0)}/10 | "
                f"Sentiment: {item.get('sentiment', 'neutral')} | "
                f"Firsthand: {firsthand_flag} | "
                f"Themes: {themes or 'none'}\n"
                f"Summary: {item.get('summary', '').strip()}"
            )

    if connections:
        parts.append("=== VERIFIED CROSS-SOURCE CONNECTIONS ===")
        for conn in connections:
            sources = conn.get("sources")
            src_a = conn.get("source_a", "").upper()
            src_b = conn.get("source_b", "").upper()
            source_label = ", ".join(str(source).upper() for source in sources) if sources else f"{src_a} × {src_b}"
            conf = conn.get("confidence", 0)
            conn_type = conn.get("connection_type", "")
            item_ids = conn.get("item_ids") or [conn.get("item_a_id"), conn.get("item_b_id")]
            item_ids = [item_id for item_id in item_ids if item_id]
            parts.append(
                f"[{source_label}] confidence={conf:.2f} type={conn_type}\n"
                f"Supporting item IDs: {', '.join(item_ids)}\n"
                f"Narrative: {conn.get('narrative', '').strip()}\n"
                f"Stock relevance: {conn.get('stock_relevance', '').strip()}"
            )
    else:
        parts.append("=== NO VERIFIED CROSS-SOURCE CONNECTIONS AVAILABLE ===")

    return "\n\n".join(parts)


CONNECTION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "valid": {"type": "boolean"},
        "rejection_reason": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "narrative": {"type": "string"},
        "stock_relevance": {"type": "string"},
        "connection_type": {
            "type": "string",
            "enum": ["causal", "corroborating", "contradicting", "leading_indicator"],
        },
    },
    "required": [
        "valid",
        "rejection_reason",
        "confidence",
        "narrative",
        "stock_relevance",
        "connection_type",
    ],
}


CONNECTION_CLUSTER_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "valid": {"type": "boolean"},
        "rejection_reason": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "narrative": {"type": "string"},
        "stock_relevance": {"type": "string"},
        "connection_type": {
            "type": "string",
            "enum": ["causal", "corroborating", "contradicting", "leading_indicator"],
        },
        "supporting_item_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "rejected_item_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "valid",
        "rejection_reason",
        "confidence",
        "narrative",
        "stock_relevance",
        "connection_type",
        "supporting_item_ids",
        "rejected_item_ids",
    ],
}


_CONNECTION_CLUSTER_SYSTEM = """\
You are a senior buy-side equity analyst reviewing a semantic cluster of 20-40 source chunks. \
The chunks were grouped by embedding similarity, not by a human theme. Your job is to decide \
whether the group contains a true business connection.

Reject weak clusters aggressively. Semantic similarity is only retrieval plumbing. It is not \
evidence. A cluster is valid only if several chunks together create a new business implication \
that a PM could act on or investigate.

MANDATORY REJECTIONS:
1. The group is mostly release announcements, reposted news, documentation updates, or roadmap \
items with no independent user/operator/developer behavior.
2. The group only says "many people mentioned the same thing." Volume is not a connection.
3. The group contains one good item plus many noisy/tangential items. Use rejected_item_ids; if \
fewer than 3 chunks remain as true support, set valid=false.
4. The group has no clear mechanism linking the evidence to revenue, margin, churn, adoption, \
pricing power, competitive position, or product quality.
5. The narrative would rely on implementation language such as semantic similarity, cluster, \
embedding, retrieved chunks, or alternative data. Never use those words in the narrative.
6. The support set is all from one source and does not contain at least 5 independent firsthand \
operator/developer/customer items. Single-source volume is usually a watchlist item, not a \
connection.
7. The support set would contain fewer than 3 genuinely useful chunks after removing noisy \
duplicates. Reject rather than stretch.

WHAT VALID LOOKS LIKE:
  - Multiple chunks independently show the same customer/developer behavior from different angles, \
and together imply a business mechanism.
  - Some chunks reveal a cause while others reveal the downstream customer/operator effect.
  - Conflicting chunks expose a useful tension: e.g. production performance is improving while \
developer reliability remains broken.

NARRATIVE: Write one PM-ready paragraph, 3-5 sentences. It should flow through: what the evidence \
shows, what else the evidence shows, what the group implies that no single chunk proves, and why \
that matters for the stock. No bullets, no section labels, no implementation language.

stock_relevance: one sentence with the direct stock mechanism.
supporting_item_ids: only the item IDs you actually used.
rejected_item_ids: noisy, duplicate, or tangential item IDs from the input.

When valid=false, set narrative="" and stock_relevance="" and explain the rejection.\
"""


_CONNECTION_SYSTEM = """\
You are a buy-side equity analyst deciding whether two alternative-data items form a genuine \
cross-source insight worth surfacing to a portfolio manager.

GOLDEN RULE: A valid connection exists only when seeing both items together reveals something \
an analyst could not learn from either item alone. If the insight is "both sources said the \
same thing," that is not a connection — it is noise. Reject it.

MANDATORY REJECTION CRITERIA — reject immediately if any apply:
1. Both items announce the same event, release, or news story. Two posts about the same ROCm \
release are the same press release appearing in two places, not a connection.
2. Neither item contains firsthand experience, original opinion, or independent user/operator \
behavior. Announcements and link reposts with no commentary carry no signal.
3. The insight reduces to "two sources reported X." Same direction + same topic = not a connection.
4. The items share a theme but produce no corroboration, no tension, no causal chain — nothing \
that would inform a question about revenue, margins, adoption, churn, or competitive position.

WHAT MAKES A CONNECTION VALID:
  - Source A reveals a cause (internal change, product regression, pricing move). Source B \
independently shows the downstream effect (customer behavior, developer response, churn signal).
  - One source shows a developer or operator frustration. Another shows a business or procurement \
consequence flowing from that same frustration.
  - A benchmark or performance claim from one community is independently corroborated by \
real-world deployment data from a different community — not just echoed.

NARRATIVE — when valid=true, write one continuous paragraph of 3–4 sentences. No numbered \
steps. No bullet points. No section headers. One flowing paragraph, written the way an analyst \
would say this out loud to a PM.

The paragraph should move naturally through: what one source observed → what the other source \
independently observed → what those two things together imply that neither alone would → why \
that matters for the stock. These are not labels, they are the natural shape of a good story.

Example of the right voice:
"Glassdoor reviews from former sales engineers describe a Q1 reorg eliminating regional account \
managers. Two months later, enterprise customers on Reddit report support response times doubling. \
The internal restructuring appears to be directly degrading customer-facing service quality — and \
management hasn't acknowledged it on any earnings call. If the support deterioration is sustained, \
it becomes a retention risk at renewal time."

Do not write: "Source A reports... Separately, Source B reports..." — that is a list, not a story. \
Do not use: "semantic similarity," "cross-source agreement," "corroboration," "alternative-data," \
"firsthand evidence," or any phrase that belongs in an engineering document rather than a brief.

stock_relevance: one sentence — the direct implication for revenue, margins, competitive \
position, or adoption. This is the "so what" extracted from the narrative for quick scanning.

When valid=false, set narrative="" and stock_relevance="" and explain the rejection briefly.\
"""


def _connection_prompt(candidate: ConnectionCandidate) -> str:
    src_a = candidate.source_a.replace("_", " ").title()
    src_b = candidate.source_b.replace("_", " ").title()

    pub_a = candidate.published_a.strftime("%Y-%m-%d") if candidate.published_a else "unknown date"
    pub_b = candidate.published_b.strftime("%Y-%m-%d") if candidate.published_b else "unknown date"

    return (
        f"TICKER: {candidate.ticker}\n\n"
        f"--- SOURCE A: {src_a} (published {pub_a}) ---\n"
        f"{(candidate.summary_a or '').strip()}\n\n"
        f"--- SOURCE B: {src_b} (published {pub_b}) ---\n"
        f"{(candidate.summary_b or '').strip()}\n\n"
        f"Do these two items together create a useful investable signal through corroboration, "
        f"tension, customer/developer behavior, or a causal explanation? Apply the rejection "
        f"criteria, but mark valid=true when the combined signal is specific enough to brief a "
        f"portfolio manager."
    )


def _connection_cluster_prompt(candidate: ConnectionClusterCandidate) -> str:
    parts = [
        f"TICKER: {candidate.ticker}",
        f"ANCHOR ITEM: {candidate.anchor_item_id}",
        f"SOURCES IN CLUSTER: {', '.join(candidate.sources)}",
        f"AVERAGE SIMILARITY: {candidate.average_similarity:.3f}",
        "",
        "SEMANTICALLY RELATED CHUNKS:",
    ]
    for index, item in enumerate(candidate.items, start=1):
        pub = item.published_at.strftime("%Y-%m-%d") if item.published_at else "unknown date"
        firsthand = "yes" if item.firsthand else "no"
        parts.append(
            f"{index}. ID: {item.source_item_id}\n"
            f"Source: {item.source} | Published: {pub} | Similarity: {item.similarity:.3f} | "
            f"Relevance: {item.relevance}/10 | Sentiment: {item.sentiment} | Firsthand: {firsthand}\n"
            f"Summary: {item.summary.strip()}"
        )
    parts.append(
        "Decide whether this semantic neighborhood contains a true multi-chunk business "
        "connection. Remove noisy chunks. Reject the cluster if it does not create a new "
        "stock-relevant implication."
    )
    return "\n\n".join(parts)
