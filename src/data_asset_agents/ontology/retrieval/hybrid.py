from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

from data_asset_agents.ontology.models import OntologyBundle
from data_asset_agents.text2sql.models import MatchedConcept


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    words.update(chinese)
    return {item for item in words if item}


def deterministic_embedding(text: str, dimensions: int = 1024) -> list[float]:
    """Stable CPU-only embedding used when no API key is configured."""

    vector = [0.0] * dimensions
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += -1.0 if digest[4] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


@dataclass(frozen=True)
class SearchDocument:
    id: str
    name: str
    kind: str
    description: str
    synonyms: list[str]

    @property
    def text(self) -> str:
        return " ".join([self.name, self.description, *self.synonyms])


def published_documents(bundle: OntologyBundle) -> list[SearchDocument]:
    documents = [
        SearchDocument(item.id, item.name, item.kind, item.description, item.synonyms)
        for item in bundle.concepts
    ]
    documents.extend(
        SearchDocument(item.id, item.name, "metric", item.description, item.synonyms)
        for item in bundle.metrics
    )
    documents.extend(
        SearchDocument(item.id, item.name, "dimension", item.description, item.synonyms)
        for item in bundle.dimensions
    )
    deduplicated: dict[tuple[str, str], SearchDocument] = {}
    for document in documents:
        deduplicated[(document.kind, document.id)] = document
    return list(deduplicated.values())


class HybridConceptRetriever:
    """Exact, synonym, keyword and deterministic vector concept retrieval."""

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions

    def search(
        self, bundle: OntologyBundle, query: str, limit: int = 10
    ) -> list[MatchedConcept]:
        query_lower = query.lower()
        query_tokens = _tokens(query)
        query_vector = deterministic_embedding(query, self.dimensions)
        results: list[MatchedConcept] = []
        for document in published_documents(bundle):
            name_lower = document.name.lower()
            exact = (
                1.0
                if name_lower == query_lower
                else (0.92 if name_lower in query_lower else 0.0)
            )
            matched_synonym = next(
                (
                    synonym
                    for synonym in document.synonyms
                    if synonym.lower() == query_lower or synonym.lower() in query_lower
                ),
                None,
            )
            synonym_score = 1.0 if matched_synonym else 0.0
            document_tokens = _tokens(document.text)
            keyword = (
                len(query_tokens & document_tokens) / len(query_tokens)
                if query_tokens
                else 0.0
            )
            document_vector = deterministic_embedding(document.text, self.dimensions)
            vector = max(
                0.0,
                sum(
                    left * right
                    for left, right in zip(
                        query_vector, document_vector, strict=True
                    )
                ),
            )
            score = min(
                1.0,
                max(exact, synonym_score * 0.95, keyword * 0.55 + vector * 0.35),
            )
            if score <= 0:
                continue
            evidence: list[str] = []
            if exact:
                evidence.append("标准名称精确匹配")
            if matched_synonym:
                evidence.append(f"同义词匹配：{matched_synonym}")
            if keyword:
                evidence.append(f"关键词覆盖率：{keyword:.3f}")
            if vector:
                evidence.append(f"向量相似度：{vector:.3f}")
            results.append(
                MatchedConcept(
                    id=document.id,
                    name=document.name,
                    kind=document.kind,
                    matched_text=matched_synonym or document.name,
                    score=round(score, 6),
                    evidence=evidence,
                    exact_score=exact,
                    synonym_score=synonym_score,
                    keyword_score=round(keyword, 6),
                    vector_score=round(vector, 6),
                )
            )
        return sorted(results, key=lambda item: (-item.score, item.name))[:limit]
