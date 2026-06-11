from __future__ import annotations

import sys
from pathlib import Path

dataload_path = str(Path(__file__).resolve().parents[2] / "dataload")
sys.path.insert(0, dataload_path)
for module_name in list(sys.modules):
    if module_name == "shared" or module_name.startswith("shared."):
        del sys.modules[module_name]

from sources.hacker_news import build_comment_body, build_story_body, clean_hn_text

sys.path.remove(dataload_path)
for module_name in list(sys.modules):
    if module_name == "shared" or module_name.startswith("shared."):
        del sys.modules[module_name]


def test_clean_hn_text_removes_html_noise() -> None:
    assert clean_hn_text("ROCm&nbsp;works<br>well<p>with <b>MI300</b>") == (
        "ROCm works\nwell\n\nwith MI300"
    )


def test_story_body_includes_title_story_url_and_comments() -> None:
    body = build_story_body(
        title="AMD ROCm deployment notes",
        story_text="We moved an inference workload to MI300X.",
        outbound_url="https://example.com/rocm",
        comments=[
            {"author": "founder", "text": "The migration was cheaper than CUDA."},
            {"author": "dev", "text": "Docs still need work."},
        ],
    )

    assert "Story title: AMD ROCm deployment notes" in body
    assert "Story text:\nWe moved an inference workload to MI300X." in body
    assert "Outbound URL: https://example.com/rocm" in body
    assert "1. founder: The migration was cheaper than CUDA." in body
    assert "2. dev: Docs still need work." in body


def test_comment_body_keeps_story_context() -> None:
    body = build_comment_body(
        title="Klaviyo integration issue",
        story_text="The story discusses email infrastructure.",
        outbound_url="https://example.com/klaviyo",
        comment={"author": "operator", "text": "We switched after deliverability degraded."},
    )

    assert "Story title: Klaviyo integration issue" in body
    assert "Story text:\nThe story discusses email infrastructure." in body
    assert "Comment by operator:\nWe switched after deliverability degraded." in body
