"""Reddit dataload implementation."""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import requests

from shared.base import DataloadRun, SourceDataloadRunner, stable_source_item_id

ARCTIC_SHIFT_BASE_URL = "https://arctic-shift.photon-reddit.com/api"
COMMENT_LIMIT_PER_POST = int(os.getenv("REDDIT_COMMENT_LIMIT_PER_POST", "10"))
POST_LIMIT_PER_QUERY = int(os.getenv("REDDIT_POST_LIMIT_PER_QUERY", "100"))

REDDIT_CONFIG: dict[str, dict[str, list[str]]] = {
    "AMD": {
        "subreddits": [
            "AMD",
            "Amd",
            "hardware",
            "buildapc",
            "MachineLearning",
            "CUDA",
            "LocalLLaMA",
            "overclocking",
            "servers",
            "homelab",
            "linux_gaming",
        ],
        "queries": ["AMD", "ROCm", "MI300", "AMD Instinct", "EPYC", "Radeon Linux"],
    },
    "SNDK": {
        "subreddits": [
            "DataHoarder",
            "hardware",
            "photography",
            "homelab",
            "videography",
            "NAS",
            "3Dprinting",
            "gopro",
            "drones",
        ],
        "queries": ["SanDisk", "Sandisk", "Western Digital", "microSD reliability"],
    },
    "FROG": {
        "subreddits": [
            "devops",
            "sysadmin",
            "kubernetes",
            "docker",
            "golang",
            "java",
            "gitlab",
            "github",
            "terraform",
            "platformengineering",
            "AWSDevOps",
            "azuredevops",
        ],
        "queries": ["JFrog", "Artifactory", "JFrog Xray"],
    },
    "APP": {
        "subreddits": [
            "gamedev",
            "androiddev",
            "adops",
            "iOSProgramming",
            "unity3d",
            "unrealengine",
            "startups",
            "mobilegaming",
        ],
        "queries": ["AppLovin", "AppLovin MAX", "Adjust"],
    },
    "KVYO": {
        "subreddits": [
            "ecommerce",
            "shopify",
            "emailmarketing",
            "Entrepreneur",
            "smallbusiness",
            "dropship",
            "marketing",
            "webdev",
            "DTC",
        ],
        "queries": ["Klaviyo", "Klaviyo Shopify", "Klaviyo email"],
    },
}


def epoch_to_datetime(value: int | float | None) -> dt.datetime | None:
    """Convert an epoch timestamp to UTC datetime."""
    if value is None:
        return None
    return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)


class RedditDataloadRunner(SourceDataloadRunner):
    """Load Arctic Shift Reddit data by source+ticker partition."""

    source = "reddit"

    def metadata(self, run: DataloadRun) -> dict[str, Any]:
        metadata = super().metadata(run)
        metadata["source_config"] = REDDIT_CONFIG.get(run.partition.ticker, {})
        metadata["post_limit_per_query"] = POST_LIMIT_PER_QUERY
        metadata["comment_limit_per_post"] = COMMENT_LIMIT_PER_POST
        return metadata

    def fetch(self, run: DataloadRun) -> list[dict[str, Any]]:
        """Fetch Reddit posts/comments for one source+ticker window."""
        cfg = REDDIT_CONFIG.get(run.partition.ticker, {})
        rows: list[dict[str, Any]] = []
        seen_posts: set[str] = set()

        for subreddit in cfg.get("subreddits", []):
            for query in cfg.get("queries", []):
                payload = self.arctic_get(
                    "posts/search",
                    {
                        "subreddit": subreddit,
                        "title": query,
                        "limit": POST_LIMIT_PER_QUERY,
                        "after": int(run.window.start.timestamp()) if run.window.start else None,
                        "before": int(run.window.end.timestamp()),
                    },
                )
                for post in payload.get("data", []) or []:
                    post_id = post.get("id")
                    created_at = epoch_to_datetime(post.get("created_utc") or post.get("created"))
                    if not post_id or post_id in seen_posts:
                        continue
                    if run.window.start and created_at and created_at < run.window.start:
                        continue
                    if created_at and created_at > run.window.end:
                        continue

                    seen_posts.add(post_id)
                    post["query"] = query
                    post["query_subreddit"] = subreddit
                    post["comments"] = self.fetch_comments(post_id)
                    rows.append(post)

        logging.info(
            "Fetched Reddit records ticker=%s posts=%d", run.partition.ticker, len(rows)
        )
        return rows

    def normalize(
        self, run: DataloadRun, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Normalize Reddit posts/comments into source_items records."""
        fetched_at = dt.datetime.now(dt.UTC)
        normalized: list[dict[str, Any]] = []

        for post in records:
            post_id = post["id"]
            permalink = post.get("permalink")
            body = post.get("selftext") or ""
            published_at = epoch_to_datetime(post.get("created_utc") or post.get("created"))
            reddit_url = f"https://www.reddit.com{permalink}" if permalink else post.get("url")

            normalized.append(
                {
                    "source_item_id": stable_source_item_id("reddit_post", post_id),
                    "ticker": run.partition.ticker,
                    "source": self.source,
                    "source_url": reddit_url,
                    "title": post.get("title"),
                    "body": body,
                    "author": post.get("author"),
                    "published_at": published_at,
                    "fetched_at": fetched_at,
                    "metadata": {
                        "kind": "post",
                        "native_id": post_id,
                        "subreddit": post.get("subreddit") or post.get("query_subreddit"),
                        "query": post.get("query"),
                        "score": post.get("score"),
                        "num_comments": post.get("num_comments"),
                        "removed_by_category": post.get("removed_by_category"),
                        "link_flair_text": post.get("link_flair_text"),
                    },
                }
            )

            for comment in post.get("comments", []):
                comment_id = comment.get("id")
                if not comment_id:
                    continue
                comment_permalink = comment.get("permalink")
                normalized.append(
                    {
                        "source_item_id": stable_source_item_id(
                            "reddit_comment", comment_id
                        ),
                        "ticker": run.partition.ticker,
                        "source": self.source,
                        "source_url": (
                            f"https://www.reddit.com{comment_permalink}"
                            if comment_permalink
                            else reddit_url
                        ),
                        "title": post.get("title"),
                        "body": comment.get("body"),
                        "author": comment.get("author"),
                        "published_at": epoch_to_datetime(
                            comment.get("created_utc") or comment.get("created")
                        ),
                        "fetched_at": fetched_at,
                        "metadata": {
                            "kind": "comment",
                            "native_id": comment_id,
                            "post_id": post_id,
                            "subreddit": comment.get("subreddit") or post.get("subreddit"),
                            "query": post.get("query"),
                            "score": comment.get("score"),
                            "distinguished": comment.get("distinguished"),
                        },
                    }
                )
        return normalized

    def arctic_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call Arctic Shift API."""
        response = requests.get(
            f"{ARCTIC_SHIFT_BASE_URL}/{path.lstrip('/')}",
            params={key: value for key, value in params.items() if value is not None},
            timeout=30,
        )
        if not response.ok:
            logging.warning(
                "Arctic Shift HTTP %s path=%s params=%s body=%s",
                response.status_code,
                path,
                params,
                response.text[:200],
            )
            return {"data": []}
        return response.json()

    def fetch_comments(self, post_id: str) -> list[dict[str, Any]]:
        """Fetch comments for one Reddit post."""
        payload = self.arctic_get(
            "comments/search", {"link_id": post_id, "limit": COMMENT_LIMIT_PER_POST}
        )
        return payload.get("data", []) or []
