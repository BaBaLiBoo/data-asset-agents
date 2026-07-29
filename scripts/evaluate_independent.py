from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.config import PROJECT_ROOT, Settings
from app.evaluation import EvaluationRunner, load_assets, load_pairs
from app.models import (
    ComparisonMode,
    ComparisonRequest,
    DatasetSplit,
    Decision,
    GoldLabel,
)
from app.service import Task1Service


DATASET_DIR = PROJECT_ROOT / "data" / "evaluation_v3"
REPORT_DIR = PROJECT_ROOT / "reports" / "evaluation_v3" / "latest"
TOP_K_VALUES = (5, 10, 20)


@dataclass(frozen=True, slots=True)
class RetrievalRecord:
    pair_id: str
    split: str
    query_asset_id: str
    duplicate_asset_id: str
    candidate_rank: int | None
    candidate_score: float | None
    pair_decision: str
    fusion_score: float
    hit_at_5: bool
    hit_at_10: bool
    hit_at_20: bool
    automatic_detection_at_5: bool
    automatic_detection_at_10: bool
    automatic_detection_at_20: bool
    review_inclusive_detection_at_5: bool
    review_inclusive_detection_at_10: bool
    review_inclusive_detection_at_20: bool


def independent_settings() -> Settings:
    source = Settings.from_env()
    return replace(
        source,
        bootstrap_assets_path=DATASET_DIR / "assets.jsonl",
        asset_catalog_path=DATASET_DIR / "asset_catalog.db",
        calibrated_thresholds_path=DATASET_DIR / "calibrated_thresholds.json",
        algorithm_version="task1_v1-independent-v3",
    )


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _retrieval_metrics(
    records: list[RetrievalRecord],
    split: DatasetSplit | None,
) -> dict[str, object]:
    selected = (
        records
        if split is None
        else [record for record in records if record.split == split.value]
    )
    total = len(selected)
    metrics: dict[str, object] = {
        "split": "ALL" if split is None else split.value,
        "duplicate_pair_count": total,
        "mean_reciprocal_rank": (
            sum(
                1.0 / record.candidate_rank
                for record in selected
                if record.candidate_rank is not None
            )
            / total
            if total
            else 0.0
        ),
        "pair_classification_recall": (
            sum(record.pair_decision == Decision.DUPLICATE.value for record in selected)
            / total
            if total
            else 0.0
        ),
    }
    for top_k in TOP_K_VALUES:
        hit_field = f"hit_at_{top_k}"
        automatic_field = f"automatic_detection_at_{top_k}"
        review_field = f"review_inclusive_detection_at_{top_k}"
        hit_count = sum(bool(getattr(record, hit_field)) for record in selected)
        automatic_count = sum(
            bool(getattr(record, automatic_field)) for record in selected
        )
        review_count = sum(bool(getattr(record, review_field)) for record in selected)
        metrics[f"candidate_hit_count_at_{top_k}"] = hit_count
        metrics[f"candidate_recall_at_{top_k}"] = hit_count / total if total else 0.0
        metrics[f"automatic_detection_count_at_{top_k}"] = automatic_count
        metrics[f"end_to_end_auto_recall_at_{top_k}"] = (
            automatic_count / total if total else 0.0
        )
        metrics[f"review_inclusive_detection_count_at_{top_k}"] = review_count
        metrics[f"end_to_end_review_inclusive_recall_at_{top_k}"] = (
            review_count / total if total else 0.0
        )
    return metrics


def evaluate_retrieval(settings: Settings) -> list[dict[str, object]]:
    assets = load_assets(DATASET_DIR / "assets.jsonl")
    pairs = load_pairs(DATASET_DIR / "pairs.csv")
    duplicate_pairs = [
        pair for pair in pairs if pair.label == GoldLabel.DUPLICATE
    ]

    # This SQLite file is a disposable V3 evaluation catalog. Recreating it
    # prevents assets from a previous dataset version contaminating retrieval.
    settings.asset_catalog_path.unlink(missing_ok=True)
    service = Task1Service.create(settings)
    if service.catalog is None or service.retriever is None:
        raise RuntimeError("catalog and retriever are required for retrieval evaluation")
    catalog_assets = service.catalog.list()
    if len(catalog_assets) != len(assets):
        raise RuntimeError(
            f"V3 catalog contains {len(catalog_assets)} assets; expected {len(assets)}"
        )

    print(
        f"Evaluating candidate recall for {len(duplicate_pairs)} duplicate pairs "
        f"against {len(catalog_assets)} catalog assets..."
    )
    service.retriever.prewarm(catalog_assets)
    records: list[RetrievalRecord] = []
    for index, pair in enumerate(duplicate_pairs, start=1):
        query = assets[pair.asset_a_id]
        target = assets[pair.asset_b_id]
        candidates, _ = service.retriever.retrieve(
            query,
            catalog_assets,
            top_k=max(TOP_K_VALUES),
            business_domain_only=True,
        )
        rank = next(
            (
                candidate_index
                for candidate_index, candidate in enumerate(candidates, start=1)
                if candidate.asset.asset_id == target.asset_id
            ),
            None,
        )
        candidate_score = next(
            (
                candidate.score
                for candidate in candidates
                if candidate.asset.asset_id == target.asset_id
            ),
            None,
        )
        comparison = service.compare(
            ComparisonRequest(
                mode=ComparisonMode.THREE_LAYER,
                asset_a=query,
                asset_b=target,
            )
        )
        if comparison.fusion_score is None:
            raise RuntimeError(f"missing fusion score for {pair.pair_id}")
        is_auto_duplicate = comparison.decision == Decision.DUPLICATE
        is_review_candidate = comparison.decision in {
            Decision.DUPLICATE,
            Decision.SUSPECTED_DUPLICATE,
        }
        hits = {
            top_k: rank is not None and rank <= top_k for top_k in TOP_K_VALUES
        }
        records.append(
            RetrievalRecord(
                pair_id=pair.pair_id,
                split=pair.split.value,
                query_asset_id=query.asset_id,
                duplicate_asset_id=target.asset_id,
                candidate_rank=rank,
                candidate_score=candidate_score,
                pair_decision=comparison.decision.value,
                fusion_score=comparison.fusion_score,
                hit_at_5=hits[5],
                hit_at_10=hits[10],
                hit_at_20=hits[20],
                automatic_detection_at_5=hits[5] and is_auto_duplicate,
                automatic_detection_at_10=hits[10] and is_auto_duplicate,
                automatic_detection_at_20=hits[20] and is_auto_duplicate,
                review_inclusive_detection_at_5=hits[5] and is_review_candidate,
                review_inclusive_detection_at_10=hits[10] and is_review_candidate,
                review_inclusive_detection_at_20=hits[20] and is_review_candidate,
            )
        )
        if index % 15 == 0 or index == len(duplicate_pairs):
            print(f"  retrieval progress: {index}/{len(duplicate_pairs)}")

    metrics = [
        _retrieval_metrics(records, DatasetSplit.DEV),
        _retrieval_metrics(records, DatasetSplit.TEST),
        _retrieval_metrics(records, None),
    ]
    _write_csv(
        REPORT_DIR / "retrieval_pair_results.csv",
        [asdict(record) for record in records],
    )
    (REPORT_DIR / "retrieval_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_retrieval_markdown(metrics)
    return metrics


def _write_retrieval_markdown(metrics: list[dict[str, object]]) -> None:
    lines = [
        "# V3候选召回与端到端检测结果",
        "",
        "| 数据集 | 重复对数 | Recall@5 | Recall@10 | Recall@20 | 自动检测@20 | 含人工复核@20 | MRR |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['split']} | {row['duplicate_pair_count']} | "
            f"{float(row['candidate_recall_at_5']):.3f} | "
            f"{float(row['candidate_recall_at_10']):.3f} | "
            f"{float(row['candidate_recall_at_20']):.3f} | "
            f"{float(row['end_to_end_auto_recall_at_20']):.3f} | "
            f"{float(row['end_to_end_review_inclusive_recall_at_20']):.3f} | "
            f"{float(row['mean_reciprocal_rank']):.3f} |"
        )
    lines.extend(
        [
            "",
            "- Recall@K：已知重复资产是否进入混合召回Top-K。",
            "- 自动检测@20：重复资产进入Top-20且三层模型自动判为DUPLICATE。",
            "- 含人工复核@20：进入Top-20且判为DUPLICATE或SUSPECTED_DUPLICATE。",
            "- TEST是正式观察口径；DEV只用于阈值选择和调试。",
            "",
        ]
    )
    (REPORT_DIR / "retrieval.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def _write_overview(metrics: list[dict[str, object]]) -> None:
    with (REPORT_DIR / "summary.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        comparison_rows = list(csv.DictReader(handle))
    test_rows = [row for row in comparison_rows if row["split"] == "TEST"]
    retrieval_test = next(row for row in metrics if row["split"] == "TEST")
    lines = [
        "# Task1独立扩充评测集V3结果",
        "",
        "> 360个半仿真资产、270对标注样本；DEV 90对，TEST 180对。"
        "生成器不读取模型分数、融合权重或阈值。该结果仍不能替代真实银行数据验证。",
        "",
        "## 一对一判重",
        "",
        "| 模式 | TEST样本 | Accuracy | Precision | Recall | F1 | 人工复核率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in test_rows:
        lines.append(
            f"| {row['mode']} | {row['pair_count']} | "
            f"{float(row['accuracy']):.3f} | {float(row['precision']):.3f} | "
            f"{float(row['recall']):.3f} | {float(row['f1']):.3f} | "
            f"{float(row['review_rate']):.3f} |"
        )
    lines.extend(
        [
            "",
            "## 全库候选召回链路",
            "",
            f"- TEST重复资产对：{retrieval_test['duplicate_pair_count']}对。",
            f"- 候选Recall@5：{float(retrieval_test['candidate_recall_at_5']):.3f}。",
            f"- 候选Recall@10：{float(retrieval_test['candidate_recall_at_10']):.3f}。",
            f"- 候选Recall@20：{float(retrieval_test['candidate_recall_at_20']):.3f}。",
            f"- 端到端自动检测Recall@20："
            f"{float(retrieval_test['end_to_end_auto_recall_at_20']):.3f}。",
            f"- 端到端含人工复核Recall@20："
            f"{float(retrieval_test['end_to_end_review_inclusive_recall_at_20']):.3f}。",
            "",
            "详细文件：`comparison.md`、`pair_results.csv`、"
            "`retrieval.md`、`retrieval_pair_results.csv`、`manifest.json`。",
            "",
        ]
    )
    (REPORT_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    settings = independent_settings()
    settings.require_embedding()
    print("Running V3 keyword baseline versus online Embedding three-layer evaluation...")
    EvaluationRunner(
        settings,
        assets_path=DATASET_DIR / "assets.jsonl",
        pairs_path=DATASET_DIR / "pairs.csv",
        report_dir=REPORT_DIR,
    ).run()
    print("Pair comparison evaluation complete.")
    metrics = evaluate_retrieval(settings)
    _write_overview(metrics)
    print(f"V3 evaluation report: {REPORT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
