from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.config import PROJECT_ROOT, Settings
from app.evaluation import load_assets
from app.keyword import KeywordComparator
from app.query_normalizer import QueryNormalizer
from app.retrieval import HybridCandidateRetriever
from app.service import Task1Service
from app.similarity_utils import clamp
from app.sql_logic import SQLFingerprint, SQLLogicComparator


DATASET_PATH = PROJECT_ROOT / "data" / "retrieval_evaluation" / "queries.jsonl"
ASSETS_PATH = PROJECT_ROOT / "data" / "evaluation_v3" / "assets.jsonl"
REPORT_DIR = PROJECT_ROOT / "reports" / "retrieval"
CONFIG_PATH = PROJECT_ROOT / "data" / "retrieval_thresholds.json"


@dataclass(frozen=True, slots=True)
class CandidateFeatures:
    asset_id: str
    embedding: float
    keyword: float
    sql_logic: float | None = None
    sql_identifier: float | None = None


@dataclass(frozen=True, slots=True)
class QueryFeatures:
    payload: dict
    candidates: tuple[CandidateFeatures, ...]
    sql_parse_failed: bool


def load_queries(path: Path = DATASET_PATH) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _weighted_available(
    components: dict[str, float | None],
    weights: dict[str, float],
) -> float:
    available = {
        key: float(value)
        for key, value in components.items()
        if value is not None and weights.get(key, 0.0) > 0
    }
    denominator = sum(weights[key] for key in available)
    if denominator <= 0:
        return 0.0
    return clamp(
        sum(weights[key] * value for key, value in available.items())
        / denominator
    )


def build_features(
    queries: list[dict],
    *,
    settings: Settings,
    embedding_client=None,
) -> list[QueryFeatures]:
    if embedding_client is None:
        service = Task1Service.create(settings)
        embedding = service.semantic.embedding_client
    else:
        embedding = embedding_client
    keyword = KeywordComparator()
    normalizer = QueryNormalizer()
    logic = SQLLogicComparator()
    assets = list(load_assets(ASSETS_PATH).values())
    asset_text = {
        asset.asset_id: HybridCandidateRetriever.retrieval_text(asset)
        for asset in assets
    }
    normalized = {
        row["query_id"]: normalizer.normalize(row["query"])
        for row in queries
    }
    all_texts = list(asset_text.values())
    for item in normalized.values():
        all_texts.append(item.original_query)
        all_texts.append(item.normalized_query)
    vectors = embedding.embed_many(all_texts)
    asset_fingerprints: dict[str, SQLFingerprint] = {
        asset.asset_id: logic.build_fingerprint(asset)
        for asset in assets
    }

    output: list[QueryFeatures] = []
    for row in queries:
        item = normalized[row["query_id"]]
        variants = list(
            dict.fromkeys(
                value
                for value in (item.original_query, item.normalized_query)
                if value.strip()
            )
        )
        query_vectors = [
            vectors[embedding._clean(value)]
            for value in variants
        ]
        sql_fingerprint = None
        sql_parse_failed = False
        if row.get("sql_text"):
            candidate = logic.build_fingerprint_from_sql(
                row["sql_text"],
                row.get("sql_dialect") or "spark",
            )
            if candidate.parse_success:
                sql_fingerprint = candidate
            else:
                sql_parse_failed = True

        eligible = assets
        domain = row.get("business_domain")
        if domain and domain in {asset.business_domain for asset in assets}:
            eligible = [
                asset for asset in assets
                if asset.business_domain == domain
            ]
        candidates: list[CandidateFeatures] = []
        for asset in eligible:
            candidate_vector = vectors[
                embedding._clean(asset_text[asset.asset_id])
            ]
            embedding_score = max(
                clamp(float(vector @ candidate_vector))
                for vector in query_vectors
            )
            keyword_score = max(
                keyword.compare_query_to_asset(value, asset)
                for value in variants
            )
            sql_logic = None
            sql_identifier = None
            asset_fingerprint = asset_fingerprints[asset.asset_id]
            if (
                sql_fingerprint is not None
                and asset_fingerprint.parse_success
            ):
                layer = logic.compare_fingerprints(
                    sql_fingerprint,
                    asset_fingerprint,
                )
                if layer.available and layer.score is not None:
                    sql_logic = float(layer.score)
                sql_identifier = logic.identifier_similarity(
                    sql_fingerprint,
                    asset_fingerprint,
                )
            candidates.append(
                CandidateFeatures(
                    asset_id=asset.asset_id,
                    embedding=embedding_score,
                    keyword=keyword_score,
                    sql_logic=sql_logic,
                    sql_identifier=sql_identifier,
                )
            )
        output.append(
            QueryFeatures(
                payload=row,
                candidates=tuple(candidates),
                sql_parse_failed=sql_parse_failed,
            )
        )
    return output


def rank(
    query: QueryFeatures,
    *,
    weights: dict[str, float],
    fallback_weights: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    active = (
        fallback_weights
        if query.sql_parse_failed and fallback_weights is not None
        else weights
    )
    result = [
        (
            candidate.asset_id,
            _weighted_available(
                {
                    "embedding": candidate.embedding,
                    "keyword": candidate.keyword,
                    "sql_logic": candidate.sql_logic,
                    "sql_identifier": candidate.sql_identifier,
                },
                active,
            ),
        )
        for candidate in query.candidates
    ]
    return sorted(result, key=lambda item: (-item[1], item[0]))


def calculate_metrics(
    features: Iterable[QueryFeatures],
    *,
    weights: dict[str, float],
    threshold: float,
    fallback_weights: dict[str, float] | None = None,
) -> dict:
    rows = list(features)
    matched_count = 0
    recall1 = 0
    recall5 = 0
    reciprocal_rank = 0.0
    no_match_count = 0
    no_match_correct = 0
    wrong_recommendations = 0
    tp = fp = fn = tn = 0
    details: list[dict] = []
    for query in rows:
        ranked = rank(
            query,
            weights=weights,
            fallback_weights=fallback_weights,
        )
        returned = [
            item for item in ranked[:5]
            if item[1] >= threshold
        ]
        expected = set(query.payload["expected_asset_ids"])
        has_match = bool(query.payload["has_match"])
        predicts_match = bool(returned)
        if has_match:
            matched_count += 1
            if predicts_match:
                tp += 1
            else:
                fn += 1
            if returned and returned[0][0] in expected:
                recall1 += 1
            rank_position = next(
                (
                    index
                    for index, (asset_id, _) in enumerate(returned, start=1)
                    if asset_id in expected
                ),
                None,
            )
            if rank_position is not None:
                recall5 += 1
                reciprocal_rank += 1.0 / rank_position
            if returned and returned[0][0] not in expected:
                wrong_recommendations += 1
        else:
            no_match_count += 1
            if predicts_match:
                fp += 1
                wrong_recommendations += 1
            else:
                tn += 1
                no_match_correct += 1
        details.append(
            {
                "query_id": query.payload["query_id"],
                "query_type": query.payload["query_type"],
                "has_match": has_match,
                "top_candidates": [
                    {"asset_id": asset_id, "score": round(score, 6)}
                    for asset_id, score in returned
                ],
                "expected_asset_ids": sorted(expected),
            }
        )
    precision = tp / (tp + fp) if tp + fp else 0.0
    binary_recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * binary_recall / (precision + binary_recall)
        if precision + binary_recall
        else 0.0
    )
    return {
        "query_count": len(rows),
        "matched_query_count": matched_count,
        "no_match_query_count": no_match_count,
        "recall_at_1": recall1 / matched_count if matched_count else 0.0,
        "recall_at_5": recall5 / matched_count if matched_count else 0.0,
        "mrr": reciprocal_rank / matched_count if matched_count else 0.0,
        "no_match_accuracy": (
            no_match_correct / no_match_count
            if no_match_count
            else None
        ),
        "error_recommendation_rate": (
            wrong_recommendations / len(rows)
            if rows
            else 0.0
        ),
        "match_precision": precision,
        "match_recall": binary_recall,
        "match_f1": f1,
        "sql_parse_failure_count": sum(
            row.sql_parse_failed for row in rows
        ),
        "details": details,
    }


def objective(metrics: dict) -> float:
    no_match = metrics["no_match_accuracy"]
    no_match_value = 1.0 if no_match is None else no_match
    return (
        0.30 * metrics["recall_at_5"]
        + 0.20 * metrics["mrr"]
        + 0.20 * no_match_value
        + 0.30 * metrics["match_f1"]
        - 0.10 * metrics["error_recommendation_rate"]
    )


def calibrate(
    features: list[QueryFeatures],
    profiles: list[dict[str, float]],
    *,
    fallback_weights: dict[str, float] | None = None,
) -> tuple[dict[str, float], float, dict]:
    best: tuple[tuple[float, ...], dict[str, float], float, dict] | None = None
    for weights in profiles:
        for step in range(0, 91):
            threshold = step / 100
            metrics = calculate_metrics(
                features,
                weights=weights,
                threshold=threshold,
                fallback_weights=fallback_weights,
            )
            key = (
                objective(metrics),
                metrics["match_precision"],
                metrics["recall_at_5"],
                -metrics["error_recommendation_rate"],
                threshold,
            )
            if best is None or key > best[0]:
                best = (key, weights, threshold, metrics)
    if best is None:
        raise ValueError("calibration requires at least one profile")
    return best[1], best[2], best[3]


def grouped_metrics(
    features: list[QueryFeatures],
    *,
    weights: dict[str, float],
    threshold: float,
    fallback_weights: dict[str, float] | None = None,
) -> dict[str, dict]:
    groups: dict[str, list[QueryFeatures]] = defaultdict(list)
    for item in features:
        groups[item.payload["query_type"]].append(item)
    return {
        name: {
            key: value
            for key, value in calculate_metrics(
                items,
                weights=weights,
                threshold=threshold,
                fallback_weights=fallback_weights,
            ).items()
            if key != "details"
        }
        for name, items in sorted(groups.items())
    }


def compact_metrics(metrics: dict) -> dict:
    return {
        key: value
        for key, value in metrics.items()
        if key != "details"
    }


def write_markdown(report: dict, path: Path) -> None:
    lines = [
        "# Task1自然语言资产检索评测",
        "",
        "> 本报告基于半仿真资产与自动生成查询，只用于工程预评测，不能替代真实用户检索评测。",
        "",
        f"- 评测时间：{report['generated_at']}",
        f"- DEV查询：{report['dataset']['dev_count']}",
        f"- TEST查询：{report['dataset']['test_count']}",
        f"- SQL解析失败/降级：{report['sql_parse_failure_count']}",
        "",
        "## TEST结果",
        "",
        "| 模式 | Recall@1 | Recall@5 | MRR | 无匹配识别率 | 错误推荐率 | 匹配F1 | 阈值 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in report["modes"].items():
        metrics = item["test"]
        no_match = metrics["no_match_accuracy"]
        no_match_text = "N/A" if no_match is None else f"{no_match:.3f}"
        lines.append(
            f"| {name} | {metrics['recall_at_1']:.3f} | "
            f"{metrics['recall_at_5']:.3f} | {metrics['mrr']:.3f} | "
            f"{no_match_text} | "
            f"{metrics['error_recommendation_rate']:.3f} | "
            f"{metrics['match_f1']:.3f} | {item['threshold']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 权重和最低阈值只在DEV上选择。",
            "- TEST只加载DEV选择结果，不参与调参。",
            "- TEXT_SQL_ENHANCED仅统计携带SQL的查询；解析失败样本按文本模式降级。",
            "- 正式三层判重的Accuracy、Precision、Recall和F1仍由`evaluate.ps1`单独评测。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    settings = Settings.from_env()
    settings.require_embedding()
    queries = load_queries()
    features = build_features(queries, settings=settings)
    dev = [item for item in features if item.payload["split"] == "DEV"]
    test = [item for item in features if item.payload["split"] == "TEST"]
    dev_sql = [item for item in dev if item.payload.get("sql_text")]
    test_sql = [item for item in test if item.payload.get("sql_text")]

    mode_profiles = {
        "KEYWORD_ONLY": [{"embedding": 0.0, "keyword": 1.0}],
        "EMBEDDING_ONLY": [{"embedding": 1.0, "keyword": 0.0}],
        "TEXT_HYBRID": [
            {"embedding": vector, "keyword": 1.0 - vector}
            for vector in (0.60, 0.70, 0.80, 0.90)
        ],
    }
    selected: dict[str, tuple[dict[str, float], float, dict]] = {}
    for name, profiles in mode_profiles.items():
        selected[name] = calibrate(dev, profiles)

    hybrid_weights = selected["TEXT_HYBRID"][0]
    sql_profiles = [
        {
            "embedding": embedding,
            "keyword": 0.10,
            "sql_logic": logic,
            "sql_identifier": 0.10,
        }
        for embedding, logic in (
            (0.45, 0.35),
            (0.50, 0.30),
            (0.55, 0.25),
        )
    ]
    selected["TEXT_SQL_ENHANCED"] = calibrate(
        dev_sql,
        sql_profiles,
        fallback_weights=hybrid_weights,
    )

    modes: dict[str, dict] = {}
    for name, (weights, threshold, dev_metrics) in selected.items():
        subset = test_sql if name == "TEXT_SQL_ENHANCED" else test
        fallback = hybrid_weights if name == "TEXT_SQL_ENHANCED" else None
        test_metrics = calculate_metrics(
            subset,
            weights=weights,
            threshold=threshold,
            fallback_weights=fallback,
        )
        modes[name] = {
            "weights": weights,
            "threshold": threshold,
            "dev": compact_metrics(dev_metrics),
            "test": compact_metrics(test_metrics),
            "test_by_query_type": grouped_metrics(
                subset,
                weights=weights,
                threshold=threshold,
                fallback_weights=fallback,
            ),
        }

    retrieval_config = {
        "text_only": {
            "vector_weight": hybrid_weights["embedding"],
            "keyword_weight": hybrid_weights["keyword"],
            "min_score": selected["TEXT_HYBRID"][1],
            "top_k": settings.text_search_default_top_k,
        },
        "text_sql_enhanced": {
            "embedding_weight": selected["TEXT_SQL_ENHANCED"][0]["embedding"],
            "keyword_weight": selected["TEXT_SQL_ENHANCED"][0]["keyword"],
            "sql_logic_weight": selected["TEXT_SQL_ENHANCED"][0]["sql_logic"],
            "sql_identifier_weight": selected["TEXT_SQL_ENHANCED"][0][
                "sql_identifier"
            ],
            "min_score": selected["TEXT_SQL_ENHANCED"][1],
            "top_k": settings.text_search_default_top_k,
        },
        "calibration": {
            "source_split": "DEV",
            "test_used_for_tuning": False,
        },
    }
    CONFIG_PATH.write_text(
        json.dumps(retrieval_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(DATASET_PATH.relative_to(PROJECT_ROOT)),
            "asset_path": str(ASSETS_PATH.relative_to(PROJECT_ROOT)),
            "dev_count": len(dev),
            "test_count": len(test),
            "dev_sql_count": len(dev_sql),
            "test_sql_count": len(test_sql),
        },
        "embedding": {
            "model": settings.embedding_model,
            "dimension": settings.embedding_dimension,
        },
        "modes": modes,
        "sql_parse_failure_count": sum(
            item.sql_parse_failed for item in features
        ),
        "limitations": [
            "资产和查询均为半仿真数据",
            "自动生成查询不能替代真实用户表达",
            "TEST未用于权重和阈值选择",
        ],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_markdown(report, REPORT_DIR / "latest.md")
    print(
        json.dumps(
            {
                "report": str(REPORT_DIR / "latest.md"),
                "config": str(CONFIG_PATH),
                "test_queries": len(test),
                "sql_parse_failure_count": report[
                    "sql_parse_failure_count"
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
