"""GitHub dataload implementation."""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import requests

from shared.base import DataloadRun, SourceDataloadRunner, stable_source_item_id
from shared.storage import parse_datetime

ISSUES_PER_REPO = int(os.getenv("GITHUB_ISSUES_PER_REPO", "100"))
RELEASES_PER_REPO = int(os.getenv("GITHUB_RELEASES_PER_REPO", "100"))

GITHUB_CONFIG: dict[str, dict[str, list[str]]] = {
    "AMD": {
        "repos": ["ROCm/ROCm", "ROCm/pytorch", "ROCm/HIP"],
    },
    "SNDK": {
        "repos": [],
    },
    "FROG": {
        "repos": ["jfrog/jfrog-cli", "jfrog/charts", "jfrog/artifactory-docker-examples"],
    },
    "APP": {
        "repos": [],
    },
    "KVYO": {
        "repos": [],
    },
}


class GitHubDataloadRunner(SourceDataloadRunner):
    """Load GitHub data by source+ticker partition."""

    source = "github"

    def metadata(self, run: DataloadRun) -> dict[str, Any]:
        metadata = super().metadata(run)
        metadata["source_config"] = GITHUB_CONFIG.get(run.partition.ticker, {})
        metadata["issues_per_repo"] = ISSUES_PER_REPO
        metadata["releases_per_repo"] = RELEASES_PER_REPO
        return metadata

    def fetch(self, run: DataloadRun) -> list[dict[str, Any]]:
        """Fetch GitHub repo/issue/release data for one source+ticker window."""
        rows: list[dict[str, Any]] = []

        for repo in GITHUB_CONFIG.get(run.partition.ticker, {}).get("repos", []):
            repo_data = self.github_get(f"repos/{repo}")
            if not repo_data.get("full_name"):
                continue
            repo_data["kind"] = "repo"
            repo_data["configured_repo"] = repo
            rows.append(repo_data)

            issue_params: dict[str, Any] = {
                "state": "all",
                "per_page": ISSUES_PER_REPO,
                "sort": "updated",
                "direction": "desc",
            }
            if run.window.start:
                issue_params["since"] = run.window.start.isoformat()

            for issue in self.github_get(f"repos/{repo}/issues", issue_params, expect_list=True):
                if "pull_request" in issue:
                    continue
                updated_at = parse_datetime(issue.get("updated_at"))
                if run.window.start and updated_at and updated_at < run.window.start:
                    continue
                issue["kind"] = "issue"
                issue["configured_repo"] = repo
                rows.append(issue)

            release_params = {"per_page": RELEASES_PER_REPO}
            for release in self.github_get(
                f"repos/{repo}/releases", release_params, expect_list=True
            ):
                published_at = parse_datetime(release.get("published_at"))
                if run.window.start and published_at and published_at < run.window.start:
                    continue
                release["kind"] = "release"
                release["configured_repo"] = repo
                rows.append(release)

        logging.info(
            "Fetched GitHub records ticker=%s records=%d",
            run.partition.ticker,
            len(rows),
        )
        return rows

    def normalize(
        self, run: DataloadRun, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Normalize GitHub records into source_items records."""
        fetched_at = dt.datetime.now(dt.UTC)
        normalized: list[dict[str, Any]] = []
        for record in records:
            kind = record["kind"]
            repo = record["configured_repo"]
            native_id = self.native_id(record)
            published_at = (
                parse_datetime(record.get("pushed_at"))
                or parse_datetime(record.get("updated_at"))
                or parse_datetime(record.get("published_at"))
                or parse_datetime(record.get("created_at"))
            )

            normalized.append(
                {
                    "source_item_id": stable_source_item_id("github", native_id),
                    "ticker": run.partition.ticker,
                    "source": self.source,
                    "source_url": record.get("html_url"),
                    "title": self.title(record),
                    "body": record.get("body") or record.get("description"),
                    "author": self.author(record),
                    "published_at": published_at,
                    "fetched_at": fetched_at,
                    "metadata": {
                        "kind": kind,
                        "native_id": native_id,
                        "repo": repo,
                        "state": record.get("state"),
                        "labels": [
                            label.get("name")
                            for label in record.get("labels", [])
                            if isinstance(label, dict)
                        ],
                        "stars": record.get("stargazers_count"),
                        "forks": record.get("forks_count"),
                        "open_issues_count": record.get("open_issues_count"),
                        "language": record.get("language"),
                        "archived": record.get("archived"),
                        "license": (record.get("license") or {}).get("spdx_id")
                        if isinstance(record.get("license"), dict)
                        else None,
                    },
                }
            )
        return normalized

    def github_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        expect_list: bool = False,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Call GitHub REST API."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = requests.get(
                f"https://api.github.com/{path.lstrip('/')}",
                headers=headers,
                params=params or {},
                timeout=15,
            )
        except requests.RequestException as exc:
            logging.warning("GitHub request failed path=%s params=%s error=%s", path, params, exc)
            return [] if expect_list else {}
        if not response.ok:
            logging.warning(
                "GitHub HTTP %s path=%s params=%s body=%s",
                response.status_code,
                path,
                params,
                response.text[:200],
            )
            return [] if expect_list else {}
        return response.json()

    def native_id(self, record: dict[str, Any]) -> str:
        """Build source-native id by GitHub record kind."""
        repo = record["configured_repo"]
        if record["kind"] == "repo":
            return f"repo:{repo}"
        if record["kind"] == "issue":
            return f"issue:{repo}:{record.get('id')}"
        return f"release:{repo}:{record.get('id') or record.get('tag_name')}"

    def title(self, record: dict[str, Any]) -> str | None:
        """Extract title by GitHub record kind."""
        if record["kind"] == "repo":
            return record.get("full_name")
        return record.get("title") or record.get("name") or record.get("tag_name")

    def author(self, record: dict[str, Any]) -> str | None:
        """Extract author by GitHub record kind."""
        user = record.get("user") or record.get("author")
        return user.get("login") if isinstance(user, dict) else None
