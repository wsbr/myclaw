"""Deterministic sparse retrieval and route fusion tests."""

from __future__ import annotations

import pytest

from app.services.sparse_retrieval_service import (
    RankedRouteHit,
    bm25_document_vector,
    bm25_query_vector,
    lexicon_vector,
    match_lexicon,
    normalize_lexicon_terms,
    reciprocal_rank_fusion,
    tokenize_for_bm25,
)


pytestmark = pytest.mark.unit


def test_bm25_tokenizer_and_vectors_are_deterministic_for_codes_and_cjk():
    tokens = tokenize_for_bm25("恢复 PAYMENT_FENCE_771 检查点")
    assert "payment_fence_771" in tokens
    assert "恢复" in tokens
    query = bm25_query_vector("PAYMENT_FENCE_771")
    document = bm25_document_vector(
        {"payment_fence_771": 2}, document_length=10,
        average_document_length=20,
        document_frequencies={"payment_fence_771": 1}, document_count=10,
    )
    assert query.indices == document.indices
    assert query.values[0] == 1.0
    assert document.values[0] > 2.0


def test_lexicon_alias_maps_to_canonical_document_term():
    terms = normalize_lexicon_terms([{
        "canonical_name": "checkpoint recovery",
        "term_type": "concept",
        "description": "resume from a durable checkpoint",
        "aliases": ["断点续跑", "检查点恢复"],
    }])
    document_matches = match_lexicon(
        "Use checkpoint recovery after a node crash.", terms,
    )
    query_matches = match_lexicon("系统如何断点续跑？", terms)
    assert document_matches[0].term_id == query_matches[0].term_id
    assert query_matches[0].matched_value == "断点续跑"
    assert lexicon_vector(document_matches).indices == lexicon_vector(query_matches).indices


def test_lexicon_rejects_ambiguous_aliases_and_rrf_keeps_route_evidence():
    with pytest.raises(ValueError, match="more than one term"):
        normalize_lexicon_terms([
            {"canonical_name": "alpha", "aliases": ["shared"]},
            {"canonical_name": "beta", "aliases": ["shared"]},
        ])
    fused = reciprocal_rank_fusion({
        "dense": [RankedRouteHit("a", 0.91), RankedRouteHit("b", 0.8)],
        "bm25": [RankedRouteHit("b", 7.2), RankedRouteHit("a", 6.1)],
        "lexicon": [RankedRouteHit("b", 1.0)],
    })
    assert fused[0].id == "b"
    assert fused[0].routes["dense"]["rank"] == 2
    assert fused[0].routes["bm25"]["rank"] == 1
    assert fused[0].routes["lexicon"]["rank"] == 1
