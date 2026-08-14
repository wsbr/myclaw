"""Versioned BM25 sparse vectors, domain lexicon matching, and RRF fusion."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import math
import re
import unicodedata
from typing import Iterable


BM25_TOKENIZER_PROFILE = "myclaw.unicode_words_cjk_bigrams.v1"
BM25_K1 = 1.2
BM25_B = 0.75
RRF_PROFILE = "myclaw.rrf.v1"
RRF_K = 60

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]", re.I)


@dataclass(frozen=True)
class SparseVector:
    indices: tuple[int, ...]
    values: tuple[float, ...]


@dataclass(frozen=True)
class LexiconMatch:
    term_id: str
    canonical_name: str
    matched_value: str
    sparse_index: int


@dataclass(frozen=True)
class RankedRouteHit:
    id: str
    score: float


@dataclass(frozen=True)
class FusedHit:
    id: str
    score: float
    routes: dict[str, dict[str, float | int]]


def normalize_term(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def tokenize_for_bm25(text: str) -> list[str]:
    """Tokenize codes/words and CJK unigrams+bigrams with a frozen profile."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    base = [match.group(0) for match in _TOKEN_PATTERN.finditer(normalized)]
    values = list(base)
    cjk_run: list[str] = []
    for item in base + [" "]:
        if len(item) == 1 and "\u3400" <= item <= "\u9fff":
            cjk_run.append(item)
            continue
        if len(cjk_run) > 1:
            values.extend(
                cjk_run[index] + cjk_run[index + 1]
                for index in range(len(cjk_run) - 1)
            )
        cjk_run = []
    return values


def sparse_index(value: str, *, namespace: str) -> int:
    digest = hashlib.blake2s(
        f"{namespace}:{value}".encode("utf-8"), digest_size=4,
    ).digest()
    return int.from_bytes(digest, "big", signed=False)


def bm25_document_vector(
    term_frequencies: dict[str, int], *, document_length: int,
    average_document_length: float, document_frequencies: dict[str, int],
    document_count: int, k1: float = BM25_K1, b: float = BM25_B,
) -> SparseVector:
    if not term_frequencies:
        return SparseVector((), ())
    average = max(float(average_document_length), 1.0)
    length = max(int(document_length), 1)
    weighted: list[tuple[int, float]] = []
    seen: dict[int, str] = {}
    for term, raw_frequency in sorted(term_frequencies.items()):
        index = sparse_index(term, namespace="bm25")
        previous = seen.get(index)
        if previous is not None and previous != term:
            raise ValueError("BM25 sparse-token hash collision")
        seen[index] = term
        frequency = max(int(raw_frequency), 0)
        if frequency == 0:
            continue
        denominator = frequency + k1 * (1.0 - b + b * length / average)
        frequency_weight = frequency * (k1 + 1.0) / denominator
        document_frequency = max(
            0, min(int(document_frequencies.get(term, 0)), document_count),
        )
        idf = math.log(
            (document_count - document_frequency + 0.5)
            / (document_frequency + 0.5) + 1.0
        )
        weight = frequency_weight * idf
        weighted.append((index, round(weight, 8)))
    weighted.sort(key=lambda item: item[0])
    return SparseVector(
        tuple(item[0] for item in weighted),
        tuple(item[1] for item in weighted),
    )


def bm25_query_vector(query: str) -> SparseVector:
    frequencies = Counter(tokenize_for_bm25(query))
    weighted = sorted(
        (
            sparse_index(term, namespace="bm25"),
            round(1.0 + math.log(frequency), 8),
        )
        for term, frequency in frequencies.items()
    )
    return SparseVector(
        tuple(item[0] for item in weighted),
        tuple(item[1] for item in weighted),
    )


def normalize_lexicon_terms(raw_terms: Iterable[dict]) -> list[dict]:
    """Validate and canonicalize an immutable lexicon revision payload."""

    terms: list[dict] = []
    claimed_values: dict[str, str] = {}
    sparse_indices: dict[int, str] = {}
    term_ids: set[str] = set()
    for raw in raw_terms:
        canonical_name = unicodedata.normalize(
            "NFKC", str(raw.get("canonical_name") or ""),
        ).strip()
        normalized_name = normalize_term(canonical_name)
        if not normalized_name or len(canonical_name) > 200:
            raise ValueError("Every lexicon term requires a canonical name of at most 200 characters")
        term_id = hashlib.sha256(f"term:{normalized_name}".encode()).hexdigest()
        if term_id in term_ids:
            raise ValueError(f"Lexicon term '{canonical_name}' is duplicated")
        term_ids.add(term_id)
        index = sparse_index(term_id, namespace="lexicon")
        previous_term = sparse_indices.get(index)
        if previous_term is not None and previous_term != term_id:
            raise ValueError("Lexicon sparse-term hash collision")
        sparse_indices[index] = term_id
        aliases = []
        for value in [canonical_name, *(raw.get("aliases") or [])]:
            display = unicodedata.normalize("NFKC", str(value or "")).strip()
            normalized = normalize_term(display)
            if not normalized or len(display) > 200:
                raise ValueError("Lexicon aliases must contain 1 to 200 characters")
            owner = claimed_values.get(normalized)
            if owner is not None and owner != term_id:
                raise ValueError(f"Lexicon value '{display}' belongs to more than one term")
            claimed_values[normalized] = term_id
            if normalized != normalized_name and normalized not in {
                item["normalized"] for item in aliases
            }:
                aliases.append({"value": display, "normalized": normalized})
        aliases.sort(key=lambda item: item["normalized"])
        terms.append({
            "id": term_id,
            "sparse_index": index,
            "canonical_name": canonical_name,
            "normalized_name": normalized_name,
            "term_type": str(raw.get("term_type") or "concept").casefold(),
            "description": str(raw.get("description") or "").strip(),
            "aliases": aliases,
        })
    terms.sort(key=lambda item: (item["normalized_name"], item["id"]))
    return terms


def match_lexicon(text: str, terms: Iterable[dict]) -> list[LexiconMatch]:
    normalized_text = normalize_term(text)
    matches: list[LexiconMatch] = []
    for term in terms:
        candidates = [{
            "value": term["canonical_name"],
            "normalized": term["normalized_name"],
        }, *(term.get("aliases") or [])]
        matched = next((
            candidate for candidate in sorted(
                candidates, key=lambda item: len(item["normalized"]), reverse=True,
            )
            if _contains_term(normalized_text, candidate["normalized"])
        ), None)
        if matched is not None:
            matches.append(LexiconMatch(
                term_id=str(term["id"]),
                canonical_name=str(term["canonical_name"]),
                matched_value=str(matched["value"]),
                sparse_index=int(term["sparse_index"]),
            ))
    return matches


def _contains_term(normalized_text: str, normalized_term: str) -> bool:
    if any("\u3400" <= char <= "\u9fff" for char in normalized_term):
        return normalized_term in normalized_text
    return re.search(
        rf"(?<![a-z0-9_]){re.escape(normalized_term)}(?![a-z0-9_])",
        normalized_text,
    ) is not None


def lexicon_vector(matches: Iterable[LexiconMatch]) -> SparseVector:
    indices = sorted({item.sparse_index for item in matches})
    return SparseVector(tuple(indices), tuple(1.0 for _ in indices))


def reciprocal_rank_fusion(
    routes: dict[str, list[RankedRouteHit]], *, k: int = RRF_K,
) -> list[FusedHit]:
    fused: dict[str, float] = {}
    evidence: dict[str, dict[str, dict[str, float | int]]] = {}
    for route_name, hits in routes.items():
        for rank, hit in enumerate(hits, 1):
            fused[hit.id] = fused.get(hit.id, 0.0) + 1.0 / (k + rank)
            evidence.setdefault(hit.id, {})[route_name] = {
                "rank": rank,
                "score": round(float(hit.score), 8),
            }
    return [FusedHit(
        id=item_id,
        score=round(score, 10),
        routes=evidence[item_id],
    ) for item_id, score in sorted(fused.items(), key=lambda item: (-item[1], item[0]))]
