from __future__ import annotations

import json
from dataclasses import dataclass, field

from .catalog import AssetCatalogError, SQLiteAssetCatalog
from .config import Settings
from .embedding import OpenAICompatibleEmbeddingClient, SQLiteEmbeddingCache
from .fusion import ThreeLayerFusion, decide_single_score
from .keyword import KeywordComparator
from .lineage import LineageComparator
from .models import (
    AssetImportResult,
    AssetInput,
    CandidateRecall,
    ComparisonMode,
    ComparisonRequest,
    ComparisonResult,
    Decision,
    DuplicateSearchItem,
    FullScanPair,
    FullScanRequest,
    FullScanResult,
    SearchDuplicatesRequest,
    SearchDuplicatesResult,
    TextAssetSearchRequest,
    TextAssetSearchResult,
    TextSearchCandidate,
    TextSearchStatus,
)
from .query_normalizer import QueryNormalizer
from .retrieval import HybridCandidateRetriever, RetrievedCandidate
from .semantic import SemanticComparator
from .sql_logic import SQLLogicComparator


@dataclass(slots=True)
class Task1Service:
    settings: Settings
    keyword: KeywordComparator
    semantic: SemanticComparator
    logic: SQLLogicComparator
    lineage: LineageComparator
    fusion: ThreeLayerFusion
    threshold_profiles: dict[ComparisonMode, dict[str, float]]
    catalog: SQLiteAssetCatalog | None = None
    retriever: HybridCandidateRetriever | None = None
    normalizer: QueryNormalizer = field(default_factory=QueryNormalizer)

    @classmethod
    def create(cls, settings: Settings) -> "Task1Service":
        settings.require_embedding()
        settings.ensure_directories()
        cache = SQLiteEmbeddingCache(settings.embedding_cache_path)
        embedding_client = OpenAICompatibleEmbeddingClient(
            api_key=settings.embedding_api_key or "",
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            send_dimensions=settings.embedding_send_dimensions,
            api_key_header=settings.embedding_api_key_header,
            api_key_prefix=settings.embedding_api_key_prefix,
            batch_size=settings.embedding_batch_size,
            timeout_seconds=settings.embedding_timeout_seconds,
            max_retries=settings.embedding_max_retries,
            cache=cache,
        )
        threshold_profiles = cls._load_threshold_profiles(settings)
        three_layer_profile = threshold_profiles[ComparisonMode.THREE_LAYER]
        keyword = KeywordComparator()
        catalog = SQLiteAssetCatalog(settings.asset_catalog_path)
        if settings.auto_bootstrap_catalog:
            catalog.bootstrap_jsonl(settings.bootstrap_assets_path, only_when_empty=True)
        logic = SQLLogicComparator()
        retrieval_profiles = cls._load_retrieval_profiles(settings)
        text_profile = retrieval_profiles["TEXT_ONLY"]
        sql_profile = retrieval_profiles["TEXT_SQL_ENHANCED"]
        retriever = HybridCandidateRetriever(
            embedding_client,
            keyword,
            vector_weight=settings.retrieval_vector_weight,
            keyword_weight=settings.retrieval_keyword_weight,
            table_weight=settings.retrieval_table_weight,
            field_weight=settings.retrieval_field_weight,
            logic=logic,
            text_embedding_weight=text_profile["embedding"],
            text_keyword_weight=text_profile["keyword"],
            text_min_score=text_profile["min_score"],
            text_sql_embedding_weight=sql_profile["embedding"],
            text_sql_keyword_weight=sql_profile["keyword"],
            text_sql_logic_weight=sql_profile["sql_logic"],
            text_sql_identifier_weight=sql_profile["sql_identifier"],
            text_sql_min_score=sql_profile["min_score"],
        )
        return cls(
            settings=settings,
            keyword=keyword,
            semantic=SemanticComparator(embedding_client),
            logic=logic,
            lineage=LineageComparator(),
            fusion=ThreeLayerFusion(
                semantic_weight=settings.semantic_weight,
                logic_weight=settings.logic_weight,
                lineage_weight=settings.lineage_weight,
                suspected_threshold=three_layer_profile["suspected"],
                duplicate_threshold=three_layer_profile["duplicate"],
                minimum_evidence_coverage=three_layer_profile["minimum_evidence_coverage"],
            ),
            threshold_profiles=threshold_profiles,
            catalog=catalog,
            retriever=retriever,
        )

    @staticmethod
    def _load_retrieval_profiles(
        settings: Settings,
    ) -> dict[str, dict[str, float]]:
        profiles = {
            "TEXT_ONLY": {
                "embedding": settings.text_search_vector_weight,
                "keyword": settings.text_search_keyword_weight,
                "min_score": settings.text_search_min_score,
            },
            "TEXT_SQL_ENHANCED": {
                "embedding": settings.sql_search_embedding_weight,
                "keyword": settings.sql_search_keyword_weight,
                "sql_logic": settings.sql_search_logic_weight,
                "sql_identifier": settings.sql_search_identifier_weight,
                "min_score": settings.sql_search_min_score,
            },
        }
        path = settings.retrieval_thresholds_path
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for mode, defaults in profiles.items():
                configured = payload.get(mode) or payload.get(mode.lower())
                if not isinstance(configured, dict):
                    continue
                configured_weights = configured.get("weights", configured)
                aliases = {
                    "embedding": ("embedding", "embedding_weight", "vector_weight"),
                    "keyword": ("keyword", "keyword_weight"),
                    "sql_logic": ("sql_logic", "sql_logic_weight"),
                    "sql_identifier": (
                        "sql_identifier",
                        "sql_identifier_weight",
                    ),
                }
                for key in tuple(defaults):
                    if key == "min_score":
                        continue
                    for alias in aliases[key]:
                        if alias in configured_weights:
                            defaults[key] = float(configured_weights[alias])
                            break
                if "min_score" in configured:
                    defaults["min_score"] = float(configured["min_score"])

        for mode, profile in profiles.items():
            min_score = profile["min_score"]
            weights = {
                key: value
                for key, value in profile.items()
                if key != "min_score"
            }
            if not 0.0 <= min_score <= 1.0:
                raise ValueError(f"invalid retrieval min_score for {mode}")
            if any(value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
                raise ValueError(f"invalid retrieval weights for {mode}")
        return profiles

    @staticmethod
    def _load_threshold_profiles(
        settings: Settings,
    ) -> dict[ComparisonMode, dict[str, float]]:
        fallback = {
            mode: {
                "suspected": settings.suspected_threshold,
                "duplicate": settings.duplicate_threshold,
                "minimum_evidence_coverage": settings.minimum_evidence_coverage,
            }
            for mode in ComparisonMode
        }
        path = settings.calibrated_thresholds_path
        if not path.exists():
            return fallback
        payload = json.loads(path.read_text(encoding="utf-8"))
        for mode in ComparisonMode:
            configured = payload.get(mode.value)
            if not configured:
                continue
            suspected = float(configured["suspected"])
            duplicate = float(configured["duplicate"])
            coverage = float(
                configured.get(
                    "minimum_evidence_coverage",
                    settings.minimum_evidence_coverage,
                )
            )
            if not 0.0 <= suspected <= duplicate <= 1.0:
                raise ValueError(f"invalid calibrated thresholds for {mode.value}")
            fallback[mode] = {
                "suspected": suspected,
                "duplicate": duplicate,
                "minimum_evidence_coverage": coverage,
            }
        return fallback

    def compare(self, request: ComparisonRequest) -> ComparisonResult:
        if request.mode == ComparisonMode.KEYWORD_BASELINE:
            keyword = self.keyword.compare(request.asset_a, request.asset_b)
            if not keyword.available or keyword.score is None:
                raise ValueError("keyword comparison has no usable evidence")
            profile = self.threshold_profiles.get(
                request.mode,
                {
                    "suspected": self.settings.suspected_threshold,
                    "duplicate": self.settings.duplicate_threshold,
                    "minimum_evidence_coverage": self.settings.minimum_evidence_coverage,
                },
            )
            decision = decide_single_score(
                keyword.score,
                suspected_threshold=profile["suspected"],
                duplicate_threshold=profile["duplicate"],
            )
            return ComparisonResult(
                mode=request.mode,
                decision=decision,
                keyword_score=keyword.score,
                evidence_coverage=keyword.quality,
                threshold_profile={
                    "suspected": profile["suspected"],
                    "duplicate": profile["duplicate"],
                },
                explanation=keyword.evidence,
                warnings=keyword.warnings,
            )

        semantic = self.semantic.compare(request.asset_a, request.asset_b)
        logic = self.logic.compare(request.asset_a, request.asset_b)
        lineage = self.lineage.compare(request.asset_a, request.asset_b)
        outcome = self.fusion.fuse(semantic, logic, lineage)
        return ComparisonResult(
            mode=request.mode,
            decision=outcome.decision,
            semantic=semantic,
            logic=logic,
            lineage=lineage,
            fusion_score=outcome.score,
            evidence_coverage=outcome.evidence_coverage,
            threshold_profile={
                "suspected": self.fusion.suspected_threshold,
                "duplicate": self.fusion.duplicate_threshold,
                "minimum_evidence_coverage": self.fusion.minimum_evidence_coverage,
            },
            explanation=outcome.explanation,
            warnings=outcome.warnings,
        )

    def import_assets(
        self,
        assets: list[AssetInput],
        *,
        replace_existing: bool = True,
        prewarm_embeddings: bool = True,
    ) -> AssetImportResult:
        catalog, retriever = self._require_search_components()
        if prewarm_embeddings:
            retriever.prewarm(assets)
        imported = catalog.upsert_many(
            assets,
            replace_existing=replace_existing,
        )
        return AssetImportResult(
            imported_count=imported,
            catalog_size=catalog.count(),
            asset_ids=[asset.asset_id for asset in assets],
        )

    def search_duplicates(
        self,
        request: SearchDuplicatesRequest,
    ) -> SearchDuplicatesResult:
        catalog, retriever = self._require_search_components()
        query = request.asset or catalog.require(request.asset_id or "")
        candidates, eligible_count = retriever.retrieve(
            query,
            catalog.list(),
            top_k=request.candidate_top_k,
            business_domain_only=request.business_domain_only,
        )
        ranked: list[DuplicateSearchItem] = []
        for candidate in candidates:
            comparison = self.compare(
                ComparisonRequest(
                    mode=ComparisonMode.THREE_LAYER,
                    asset_a=query,
                    asset_b=candidate.asset,
                )
            )
            if (
                not request.include_not_duplicate
                and comparison.decision == Decision.NOT_DUPLICATE
            ):
                continue
            ranked.append(self._search_item(candidate, comparison))
        priority = {
            Decision.DUPLICATE: 2,
            Decision.SUSPECTED_DUPLICATE: 1,
            Decision.NOT_DUPLICATE: 0,
        }
        ranked.sort(
            key=lambda item: (
                -priority[item.comparison.decision],
                -(item.comparison.fusion_score or 0.0),
                -item.candidate.recall_score,
                item.candidate.asset_id,
            )
        )
        results = ranked[: request.result_top_k]
        return SearchDuplicatesResult(
            query_asset_id=query.asset_id,
            query_asset_name=query.asset_name,
            catalog_size=catalog.count(),
            eligible_candidates=eligible_count,
            retrieved_candidates=len(candidates),
            returned_candidates=len(results),
            results=results,
        )

    def search_by_text(
        self,
        request: TextAssetSearchRequest,
    ) -> TextAssetSearchResult:
        catalog, retriever = self._require_search_components()
        normalized = self.normalizer.normalize(request.query)
        outcome = retriever.retrieve_by_text(
            normalized.original_query,
            normalized.normalized_query,
            catalog.list(),
            top_k=request.top_k,
            business_domain=request.business_domain,
            sql_text=request.sql_text,
            sql_dialect=request.sql_dialect,
        )
        candidates = [
            TextSearchCandidate(
                asset_id=item.asset.asset_id,
                asset_name=item.asset.asset_name,
                description=item.asset.description[:200],
                business_domain=item.asset.business_domain,
                recall_score=item.score,
                score_components=item.components,
            )
            for item in outcome.candidates
        ]
        return TextAssetSearchResult(
            status=(
                TextSearchStatus.MATCHED
                if candidates
                else TextSearchStatus.NO_MATCH
            ),
            retrieval_mode=outcome.mode,
            original_query=request.query,
            normalized_query=normalized.normalized_query,
            normalization_method=normalized.method,
            business_domain_applied=outcome.applied_business_domain,
            catalog_size=catalog.count(),
            eligible_assets=outcome.eligible_asset_count,
            min_score=outcome.min_score,
            returned_candidates=len(candidates),
            candidates=candidates,
            warnings=outcome.warnings,
        )

    def scan_all(self, request: FullScanRequest) -> FullScanResult:
        catalog, retriever = self._require_search_components()
        assets = catalog.list()
        if request.max_assets is not None:
            assets = assets[: request.max_assets]
        asset_by_id = {asset.asset_id: asset for asset in assets}
        naive_pair_count = len(assets) * (len(assets) - 1) // 2
        candidate_pairs: dict[tuple[str, str], RetrievedCandidate] = {}
        directed_candidate_count = 0
        for query in assets:
            candidates, _ = retriever.retrieve(
                query,
                assets,
                top_k=request.candidate_top_k,
                business_domain_only=request.business_domain_only,
            )
            directed_candidate_count += len(candidates)
            for candidate in candidates:
                pair = tuple(sorted((query.asset_id, candidate.asset.asset_id)))
                previous = candidate_pairs.get(pair)
                if previous is None or candidate.score > previous.score:
                    candidate_pairs[pair] = candidate

        pairs: list[FullScanPair] = []
        duplicate_count = 0
        suspected_count = 0
        for (left_id, right_id), recalled in sorted(candidate_pairs.items()):
            left = asset_by_id[left_id]
            right = asset_by_id[right_id]
            comparison = self.compare(
                ComparisonRequest(
                    mode=ComparisonMode.THREE_LAYER,
                    asset_a=left,
                    asset_b=right,
                )
            )
            if comparison.decision == Decision.DUPLICATE:
                duplicate_count += 1
            elif comparison.decision == Decision.SUSPECTED_DUPLICATE:
                suspected_count += 1
            include = (
                comparison.decision == Decision.DUPLICATE
                or (
                    request.include_suspected
                    and comparison.decision == Decision.SUSPECTED_DUPLICATE
                )
                or request.include_not_duplicate
            )
            if include:
                pairs.append(
                    FullScanPair(
                        asset_a_id=left.asset_id,
                        asset_a_name=left.asset_name,
                        asset_b_id=right.asset_id,
                        asset_b_name=right.asset_name,
                        recall_score=recalled.score,
                        recall_components=recalled.components,
                        comparison=comparison,
                    )
                )
        pairs.sort(
            key=lambda item: (
                -(item.comparison.fusion_score or 0.0),
                item.asset_a_id,
                item.asset_b_id,
            )
        )
        compared = len(candidate_pairs)
        reduction_rate = (
            max(0.0, min(1.0, 1.0 - compared / naive_pair_count))
            if naive_pair_count
            else 0.0
        )
        return FullScanResult(
            catalog_size=catalog.count(),
            scanned_assets=len(assets),
            naive_pair_count=naive_pair_count,
            candidate_pair_count=directed_candidate_count,
            compared_pair_count=compared,
            comparison_reduction_rate=reduction_rate,
            duplicate_count=duplicate_count,
            suspected_count=suspected_count,
            returned_pair_count=len(pairs),
            pairs=pairs,
        )

    @staticmethod
    def _search_item(
        candidate: RetrievedCandidate,
        comparison: ComparisonResult,
    ) -> DuplicateSearchItem:
        return DuplicateSearchItem(
            candidate=CandidateRecall(
                asset_id=candidate.asset.asset_id,
                asset_name=candidate.asset.asset_name,
                recall_score=candidate.score,
                components=candidate.components,
            ),
            comparison=comparison,
        )

    def _require_search_components(
        self,
    ) -> tuple[SQLiteAssetCatalog, HybridCandidateRetriever]:
        if self.catalog is None or self.retriever is None:
            raise AssetCatalogError("asset catalog and candidate retriever are not configured")
        return self.catalog, self.retriever

    @property
    def embedding_model(self) -> str:
        return self.settings.embedding_model

    @property
    def embedding_dimension(self) -> int:
        return self.settings.embedding_dimension
