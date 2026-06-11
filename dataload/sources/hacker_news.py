"""Hacker News dataload implementation."""

from __future__ import annotations

import collections
import datetime as dt
import html
import logging
import os
import re
from typing import Any

import requests

from shared.base import DataloadRun, SourceDataloadRunner, stable_source_item_id
from shared.storage import parse_datetime

ALGOLIA_HN_BASE_URL = "https://hn.algolia.com/api/v1"
HITS_PER_QUERY = int(os.getenv("HN_HITS_PER_QUERY", "100"))
PAGES_PER_QUERY = int(os.getenv("HN_PAGES_PER_QUERY", "5"))
COMMENT_LIMIT_PER_STORY = int(os.getenv("HN_COMMENT_LIMIT_PER_STORY", "10"))

HN_CONFIG: dict[str, dict[str, list[str]]] = {
    "AMD": {
        "queries": ["ROCm", "AMD ROCm", "MI300", "AMD Instinct", "EPYC", "AMD GPU"],
    },
    "SNDK": {
        "queries": [
            "SanDisk reliability",
            "Western Digital SSD",
            "WD SSD",
            "microSD reliability",
            "SanDisk SD card",
        ],
    },
    "FROG": {
        "queries": ["JFrog", "Artifactory", "JFrog Xray", "artifact registry"],
    },
    "APP": {
        "queries": ["AppLovin", "AppLovin MAX", "mobile ad mediation"],
    },
    "KVYO": {
        "queries": ["Klaviyo", "Klaviyo Shopify", "Klaviyo email"],
    },
}


class HackerNewsDataloadRunner(SourceDataloadRunner):
    """Load Hacker News data by source+ticker partition."""

    source = "hacker_news"

    def metadata(self, run: DataloadRun) -> dict[str, Any]:
        metadata = super().metadata(run)
        metadata["source_config"] = HN_CONFIG.get(run.partition.ticker, {})
        metadata["hits_per_query"] = HITS_PER_QUERY
        metadata["pages_per_query"] = PAGES_PER_QUERY
        metadata["comment_limit_per_story"] = COMMENT_LIMIT_PER_STORY
        return metadata

    def fetch(self, run: DataloadRun) -> list[dict[str, Any]]:
        """Fetch Hacker News stories/comments for one source+ticker window."""
        cfg = HN_CONFIG.get(run.partition.ticker, {})
        rows: list[dict[str, Any]] = []
        seen_story_ids: set[str] = set()

        for query in cfg.get("queries", []):
            for page in range(PAGES_PER_QUERY):
                payload = self.hn_get(
                    "search_by_date",
                    {
                        "query": query,
                        "tags": "story",
                        "hitsPerPage": HITS_PER_QUERY,
                        "page": page,
                        "numericFilters": self.numeric_filters(run),
                    },
                )
                hits = payload.get("hits", []) or []
                if not hits:
                    break
                for hit in hits:
                    story_id = hit.get("objectID") or hit.get("story_id")
                    created_at = parse_datetime(hit.get("created_at"))
                    if not story_id or story_id in seen_story_ids:
                        continue
                    if run.window.start and created_at and created_at < run.window.start:
                        continue
                    if created_at and created_at > run.window.end:
                        continue

                    seen_story_ids.add(story_id)
                    hit["query"] = query
                    hit["comments"] = (
                        self.fetch_comments(story_id)
                        if (hit.get("num_comments") or 0) > 0
                        else []
                    )
                    rows.append(hit)

        logging.info(
            "Fetched Hacker News records ticker=%s stories=%d",
            run.partition.ticker,
            len(rows),
        )
        return rows

    def normalize(
        self, run: DataloadRun, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Normalize Hacker News stories/comments into source_items records."""
        fetched_at = dt.datetime.now(dt.UTC)
        normalized: list[dict[str, Any]] = []

        for story in records:
            story_id = story.get("objectID") or story.get("story_id")
            title = story.get("title") or story.get("story_title")
            story_url = story.get("url") or story.get("story_url")
            hn_url = f"https://news.ycombinator.com/item?id={story_id}"
            published_at = parse_datetime(story.get("created_at"))
            story_text = clean_hn_text(story.get("story_text") or story.get("text"))
            comments = story.get("comments", [])
            story_body = build_story_body(
                title=title,
                story_text=story_text,
                outbound_url=story_url,
                comments=comments,
            )

            normalized.append(
                {
                    "source_item_id": stable_source_item_id("hn_story", str(story_id)),
                    "ticker": run.partition.ticker,
                    "source": self.source,
                    "source_url": hn_url,
                    "title": title,
                    "body": story_body,
                    "author": story.get("author"),
                    "published_at": published_at,
                    "fetched_at": fetched_at,
                    "metadata": {
                        "kind": "story",
                        "native_id": story_id,
                        "query": story.get("query"),
                        "outbound_url": story_url,
                        "points": story.get("points"),
                        "num_comments": story.get("num_comments"),
                        "comment_count_fetched": len(comments),
                        "has_story_text": bool(story_text),
                        "tags": story.get("_tags"),
                    },
                }
            )

            for comment in story.get("comments", []):
                comment_id = comment.get("id")
                if not comment_id:
                    continue
                normalized.append(
                    {
                        "source_item_id": stable_source_item_id(
                            "hn_comment", str(comment_id)
                        ),
                        "ticker": run.partition.ticker,
                        "source": self.source,
                        "source_url": hn_url,
                        "title": title,
                        "body": build_comment_body(
                            title=title,
                            story_text=story_text,
                            outbound_url=story_url,
                            comment=comment,
                        ),
                        "author": comment.get("author"),
                        "published_at": parse_datetime(comment.get("created_at")),
                        "fetched_at": fetched_at,
                        "metadata": {
                            "kind": "comment",
                            "native_id": comment_id,
                            "story_id": story_id,
                            "query": story.get("query"),
                            "has_story_text": bool(story_text),
                        },
                    }
                )
        return normalized

    def hn_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call Algolia Hacker News API."""
        try:
            response = requests.get(
                f"{ALGOLIA_HN_BASE_URL}/{path.lstrip('/')}",
                params=params or {},
                timeout=15,
            )
        except requests.RequestException as exc:
            logging.warning("HN request failed path=%s params=%s error=%s", path, params, exc)
            return {}
        if not response.ok:
            logging.warning(
                "HN HTTP %s path=%s params=%s body=%s",
                response.status_code,
                path,
                params,
                response.text[:200],
            )
            return {}
        return response.json()

    def numeric_filters(self, run: DataloadRun) -> str:
        """Build Algolia timestamp filters for the run window."""
        filters = [f"created_at_i<={int(run.window.end.timestamp())}"]
        if run.window.start:
            filters.append(f"created_at_i>={int(run.window.start.timestamp())}")
        return ",".join(filters)

    def fetch_comments(self, story_id: str) -> list[dict[str, Any]]:
        """Fetch a bounded set of comments for one HN story."""
        payload = self.hn_get(f"items/{story_id}")
        comments: list[dict[str, Any]] = []
        queue: collections.deque[Any] = collections.deque(payload.get("children") or [])
        while queue and len(comments) < COMMENT_LIMIT_PER_STORY:
            comment = queue.popleft()
            if not isinstance(comment, dict):
                continue
            comments.append(comment)
            queue.extend(comment.get("children") or [])
        return comments


def clean_hn_text(value: Any) -> str:
    """Convert Algolia/HN HTML-ish text into readable plain text."""
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = text.replace("\xa0", " ")
    text = re.sub(r"<p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_story_body(
    title: str | None,
    story_text: str,
    outbound_url: str | None,
    comments: list[dict[str, Any]],
) -> str:
    """Build the full story text used by enrichment, search, and connections."""
    sections = []
    if title:
        sections.append(f"Story title: {title}")
    if story_text:
        sections.append(f"Story text:\n{story_text}")
    if outbound_url:
        sections.append(f"Outbound URL: {outbound_url}")

    comment_lines = []
    for idx, comment in enumerate(comments, start=1):
        text = clean_hn_text(comment.get("text"))
        if not text:
            continue
        author = comment.get("author") or "unknown"
        comment_lines.append(f"{idx}. {author}: {text}")
    if comment_lines:
        sections.append("Fetched comment thread:\n" + "\n\n".join(comment_lines))

    return "\n\n".join(sections).strip()


def build_comment_body(
    title: str | None,
    story_text: str,
    outbound_url: str | None,
    comment: dict[str, Any],
) -> str:
    """Preserve story context alongside each normalized HN comment."""
    sections = []
    if title:
        sections.append(f"Story title: {title}")
    if story_text:
        sections.append(f"Story text:\n{story_text}")
    if outbound_url:
        sections.append(f"Outbound URL: {outbound_url}")
    comment_text = clean_hn_text(comment.get("text"))
    if comment_text:
        author = comment.get("author") or "unknown"
        sections.append(f"Comment by {author}:\n{comment_text}")
    return "\n\n".join(sections).strip()
