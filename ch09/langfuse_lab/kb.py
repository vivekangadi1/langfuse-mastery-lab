"""Tiny keyword retriever over the help-center articles."""
from __future__ import annotations

import re

STOPWORDS = {
    "a", "an", "and", "are", "can", "do", "does", "for", "how", "i", "in", "is", "it", "my", "of",
    "on", "or", "the", "to", "what", "when", "where", "why", "with", "you", "your",
}


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]


def search(query: str, articles: list[dict], k: int = 3) -> list[dict]:
    terms = set(tokenize(query))
    scored = []
    for article in articles:
        score = sum(1 for word in tokenize(f"{article['title']} {article['body']}") if word in terms)
        if score:
            scored.append((score, article["id"], article))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"id": a["id"], "title": a["title"], "body": a["body"], "score": s} for s, _, a in scored[:k]]
