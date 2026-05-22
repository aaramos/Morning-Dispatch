from __future__ import annotations

import re

from backend.agents.librarian.articles import ArticleFetchResult


def keyword_set(text: str) -> set[str]:
    return {word for word in tokens(text) if word not in STOPWORDS and len(word) > 1}


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z0-9']+", text.lower())


def fallback_text(result: ArticleFetchResult) -> str:
    metadata = result.payload.metadata or {}
    return " ".join(
        str(value)
        for value in (
            metadata.get("link_text"),
            metadata.get("title"),
            metadata.get("parent_subject"),
            metadata.get("subject"),
            result.original_url,
        )
        if value
    )


STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "because",
    "been",
    "being",
    "but",
    "can",
    "could",
    "from",
    "had",
    "has",
    "have",
    "how",
    "into",
    "its",
    "more",
    "new",
    "not",
    "now",
    "only",
    "our",
    "out",
    "over",
    "said",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "through",
    "today",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
    "you",
    "your",
}
