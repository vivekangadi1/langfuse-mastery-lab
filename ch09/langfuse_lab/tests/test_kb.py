from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.kb import search, tokenize


def test_tokenize_drops_stopwords():
    assert tokenize("What is the refund policy?") == ["refund", "policy"]


def test_search_ranks_refund_article_first():
    hits = search("refund policy for double charge", load_fixtures().kb, k=3)
    assert hits and hits[0]["id"] == "kb_refunds"
    assert len(hits) <= 3 and all(h["score"] > 0 for h in hits)


def test_search_no_match_returns_empty():
    assert search("zzzz qqqq", load_fixtures().kb) == []
