from __future__ import annotations

import re
from dataclasses import dataclass

from .embedding import OpenAICompatibleEmbeddingClient
from .keyword import KeywordComparator, tokenize
from .models import AssetInput, TextRetrievalMode
from .semantic import SemanticComparator
from .similarity_utils import clamp, jaccard
from .sql_logic import SQLLogicComparator


@dataclass(frozen=True, slots=True)
class RetrievedCandidate:
    asset: AssetInput
    score: float
    components: dict[str, float]


@dataclass(frozen=True, slots=True)
class TextRetrievalOutcome:
    candidates: list[RetrievedCandidate]
    eligible_asset_count: int
    mode: TextRetrievalMode
    applied_business_domain: str | None
    min_score: float
    warnings: list[str]


class HybridCandidateRetriever:
    """High-recall first stage for the demo catalog.

    This implementation scans catalog vectors in memory. It is intentionally
    behind a small interface so Milvus/pgvector can replace it in production.
    Only the returned Top-K candidates enter the expensive three-layer rerank.
    """

    def __init__(
        self,
        embedding_client: OpenAICompatibleEmbeddingClient,
        keyword: KeywordComparator,
        *,
        vector_weight: float = 0.60,
        keyword_weight: float = 0.20,
        table_weight: float = 0.10,
        field_weight: float = 0.10,
        logic: SQLLogicComparator | None = None,
        text_embedding_weight: float = 0.80,
        text_keyword_weight: float = 0.20,
        text_min_score: float = 0.45,
        text_sql_embedding_weight: float = 0.50,
        text_sql_keyword_weight: float = 0.10,
        text_sql_logic_weight: float = 0.30,
        text_sql_identifier_weight: float = 0.10,
        text_sql_min_score: float = 0.55,
    ) -> None:
        weights = {
            "vector": float(vector_weight),
            "keyword": float(keyword_weight),
            "table": float(table_weight),
            "field": float(field_weight),
        }
        if any(weight < 0 for weight in weights.values()):
            raise ValueError("retrieval weights cannot be negative")
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("at least one retrieval weight must be positive")
        self.embedding_client = embedding_client
        self.keyword = keyword
        self.weights = {key: value / total for key, value in weights.items()}
        self.logic = logic or SQLLogicComparator()
        self.text_weights = self._normalized_weights(
            {
                "embedding": text_embedding_weight,
                "keyword": text_keyword_weight,
            }
        )
        self.text_sql_weights = self._normalized_weights(
            {
                "embedding": text_sql_embedding_weight,
                "keyword": text_sql_keyword_weight,
                "sql_logic": text_sql_logic_weight,
                "sql_identifier": text_sql_identifier_weight,
            }
        )
        self.text_min_score = self._validate_threshold(text_min_score)
        self.text_sql_min_score = self._validate_threshold(text_sql_min_score)

    @staticmethod
    def _normalized_weights(weights: dict[str, float]) -> dict[str, float]:
        if any(float(value) < 0 for value in weights.values()):
            raise ValueError("retrieval weights cannot be negative")
        total = sum(float(value) for value in weights.values())
        if total <= 0:
            raise ValueError("at least one retrieval weight must be positive")
        return {key: float(value) / total for key, value in weights.items()}

    @staticmethod
    def _validate_threshold(value: float) -> float:
        threshold = float(value)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("retrieval threshold must be between 0 and 1")
        return threshold

    @staticmethod
    def retrieval_text(asset: AssetInput) -> str:
        blocks = SemanticComparator.text_blocks(asset)
        text = " | ".join(
            f"{key}:{value}" for key, value in blocks.items() if value.strip()
        )
        # Avoid submitting unexpectedly large schema text to the online service.
        return text[:16_000]

    @staticmethod
    def _table_terms(asset: AssetInput) -> set[str]:
        terms: set[str] = set()
        for table in asset.tables:
            for value in (table.name, table.cn_name):
                terms.update(tokenize(value))
        return terms

    @staticmethod
    def _field_terms(asset: AssetInput) -> set[str]:
        terms: set[str] = set()
        for table in asset.tables:
            for column in table.columns:
                for value in (column.name, column.cn_name):
                    terms.update(tokenize(value))
        return terms

    def prewarm(self, assets: list[AssetInput]) -> None:
        self.embedding_client.embed_many(
            [self.retrieval_text(asset) for asset in assets]
        )

    def retrieve(
        self,
        query: AssetInput,
        assets: list[AssetInput],
        *,
        top_k: int,
        business_domain_only: bool = True,
    ) -> tuple[list[RetrievedCandidate], int]:
        eligible = [
            asset
            for asset in assets
            if asset.asset_id != query.asset_id
            and (
                not business_domain_only
                or asset.business_domain == query.business_domain
            )
        ]
        if not eligible:
            return [], 0

        query_text = self.retrieval_text(query)
        candidate_texts = [self.retrieval_text(asset) for asset in eligible]
        vectors = self.embedding_client.embed_many([query_text, *candidate_texts])
        query_vector = vectors[self.embedding_client._clean(query_text)]
        query_tables = self._table_terms(query)
        query_fields = self._field_terms(query)

        result: list[RetrievedCandidate] = []
        for asset, candidate_text in zip(eligible, candidate_texts, strict=True):
            clean_text = self.embedding_client._clean(candidate_text)
            vector_score = clamp(float(query_vector @ vectors[clean_text]))
            keyword_result = self.keyword.compare(query, asset)
            keyword_score = (
                float(keyword_result.score)
                if keyword_result.available and keyword_result.score is not None
                else 0.0
            )
            table_score = jaccard(query_tables, self._table_terms(asset))
            field_score = jaccard(query_fields, self._field_terms(asset))
            components = {
                "vector": vector_score,
                "keyword": keyword_score,
                "table": float(table_score or 0.0),
                "field": float(field_score or 0.0),
            }
            score = clamp(
                sum(self.weights[key] * components[key] for key in self.weights)
            )
            result.append(
                RetrievedCandidate(
                    asset=asset,
                    score=score,
                    components=components,
                )
            )
        result.sort(key=lambda item: (-item.score, item.asset.asset_id))
        return result[: min(top_k, len(result))], len(eligible)

    @staticmethod
    def _weighted_available(
        components: dict[str, float],
        weights: dict[str, float],
    ) -> float:
        available_weight = sum(
            weights[key] for key in components if key in weights
        )
        if available_weight <= 0:
            return 0.0
        return clamp(
            sum(
                weights[key] * value
                for key, value in components.items()
                if key in weights
            )
            / available_weight
        )

    def retrieve_by_text(
        self,
        original_query: str,
        normalized_query: str,
        assets: list[AssetInput],
        *,
        top_k: int = 5,
        business_domain: str | None = None,
        sql_text: str | None = None,
        sql_dialect: str = "spark",
    ) -> TextRetrievalOutcome:
        warnings: list[str] = []
        applied_domain: str | None = None
        eligible = list(assets)
        if business_domain:
            known_domains = {asset.business_domain for asset in assets}
            if business_domain in known_domains:
                applied_domain = business_domain
                eligible = [
                    asset
                    for asset in assets
                    if asset.business_domain == business_domain
                ]
            else:
                warnings.append(
                    f"业务域“{business_domain}”不存在，已回退到全目录检索"
                )

        mode = TextRetrievalMode.TEXT_ONLY
        active_weights = self.text_weights
        min_score = self.text_min_score
        query_fingerprint = None
        if sql_text:
            candidate = self.logic.build_fingerprint_from_sql(
                sql_text,
                sql_dialect,
            )
            if candidate.parse_success:
                query_fingerprint = candidate
                mode = TextRetrievalMode.TEXT_SQL_ENHANCED
                active_weights = self.text_sql_weights
                min_score = self.text_sql_min_score
            else:
                mode = TextRetrievalMode.TEXT_ONLY_FALLBACK
                warning = re.sub(
                    r"\x1b\[[0-9;]*m",
                    "",
                    candidate.warning or "未知解析错误",
                )
                warnings.append(
                    "输入SQL解析失败，已降级为纯自然语言检索："
                    f"{warning}"
                )

        if not eligible:
            return TextRetrievalOutcome(
                candidates=[],
                eligible_asset_count=0,
                mode=mode,
                applied_business_domain=applied_domain,
                min_score=min_score,
                warnings=warnings,
            )

        query_variants = list(
            dict.fromkeys(
                value.strip()
                for value in (original_query, normalized_query)
                if value.strip()
            )
        )
        candidate_texts = [self.retrieval_text(asset) for asset in eligible]
        vectors = self.embedding_client.embed_many(
            [*query_variants, *candidate_texts]
        )
        query_vectors = [
            vectors[self.embedding_client._clean(value)]
            for value in query_variants
        ]

        result: list[RetrievedCandidate] = []
        for asset, candidate_text in zip(eligible, candidate_texts, strict=True):
            candidate_vector = vectors[
                self.embedding_client._clean(candidate_text)
            ]
            embedding_score = max(
                clamp(float(query_vector @ candidate_vector))
                for query_vector in query_vectors
            )
            keyword_score = max(
                self.keyword.query_score(query, asset)
                for query in query_variants
            )
            components = {
                "embedding": embedding_score,
                "keyword": keyword_score,
            }

            if query_fingerprint is not None:
                asset_fingerprint = self.logic.build_fingerprint(asset)
                if asset_fingerprint.parse_success:
                    logic_result = self.logic.compare_fingerprints(
                        query_fingerprint,
                        asset_fingerprint,
                    )
                    if (
                        logic_result.available
                        and logic_result.score is not None
                    ):
                        components["sql_logic"] = float(logic_result.score)
                    identifier_score = self.logic.identifier_similarity(
                        query_fingerprint,
                        asset_fingerprint,
                    )
                    if identifier_score is not None:
                        components["sql_identifier"] = float(identifier_score)

            score = self._weighted_available(components, active_weights)
            if score >= min_score:
                result.append(
                    RetrievedCandidate(
                        asset=asset,
                        score=score,
                        components={
                            key: clamp(value)
                            for key, value in components.items()
                        },
                    )
                )

        result.sort(key=lambda item: (-item.score, item.asset.asset_id))
        return TextRetrievalOutcome(
            candidates=result[: min(top_k, len(result))],
            eligible_asset_count=len(eligible),
            mode=mode,
            applied_business_domain=applied_domain,
            min_score=min_score,
            warnings=warnings,
        )
