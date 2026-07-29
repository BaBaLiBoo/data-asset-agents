from __future__ import annotations

import csv
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT, Settings
from .models import (
    AssetInput,
    ComparisonMode,
    DatasetSplit,
    Decision,
    EvaluationPair,
    GoldLabel,
)
from .semantic import COMPONENT_WEIGHTS as SEMANTIC_COMPONENT_WEIGHTS
from .service import Task1Service


@dataclass(frozen=True, slots=True)
class ThresholdProfile:
    suspected: float
    duplicate: float
    minimum_evidence_coverage: float


@dataclass(frozen=True, slots=True)
class ScoreRecord:
    pair_id: str
    family_id: str
    split: DatasetSplit
    scenario: str
    label: GoldLabel
    mode: ComparisonMode
    score: float
    evidence_coverage: float
    semantic_score: float | None
    logic_score: float | None
    lineage_score: float | None
    latency_ms: float


def load_assets(path: Path) -> dict[str, AssetInput]:
    assets = [
        AssetInput.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result = {asset.asset_id: asset for asset in assets}
    if len(result) != len(assets):
        raise ValueError("duplicate asset_id in assets.jsonl")
    return result


def load_pairs(path: Path) -> list[EvaluationPair]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        pairs = [EvaluationPair.model_validate(row) for row in csv.DictReader(handle)]
    if len({pair.pair_id for pair in pairs}) != len(pairs):
        raise ValueError("duplicate pair_id in pairs.csv")
    family_splits: dict[str, DatasetSplit] = {}
    for pair in pairs:
        previous = family_splits.setdefault(pair.family_id, pair.split)
        if previous != pair.split:
            raise ValueError(f"family split leakage: {pair.family_id}")
    return pairs


def apply_threshold(record: ScoreRecord, profile: ThresholdProfile) -> Decision:
    coverage_ok = (
        record.mode == ComparisonMode.KEYWORD_BASELINE
        or record.evidence_coverage >= profile.minimum_evidence_coverage
    )
    if record.score >= profile.duplicate and coverage_ok:
        return Decision.DUPLICATE
    if record.score >= profile.suspected:
        return Decision.SUSPECTED_DUPLICATE
    return Decision.NOT_DUPLICATE


def classification_metrics(
    records: list[ScoreRecord],
    profile: ThresholdProfile,
) -> dict[str, float | int]:
    decisions = [(record, apply_threshold(record, profile)) for record in records]
    tp = sum(record.label == GoldLabel.DUPLICATE and decision == Decision.DUPLICATE for record, decision in decisions)
    fp = sum(record.label == GoldLabel.NOT_DUPLICATE and decision == Decision.DUPLICATE for record, decision in decisions)
    fn = sum(record.label == GoldLabel.DUPLICATE and decision != Decision.DUPLICATE for record, decision in decisions)
    tn = sum(record.label == GoldLabel.NOT_DUPLICATE and decision == Decision.NOT_DUPLICATE for record, decision in decisions)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    duplicate_total = sum(record.label == GoldLabel.DUPLICATE for record in records)
    candidate_hits = sum(
        record.label == GoldLabel.DUPLICATE
        and decision in {Decision.DUPLICATE, Decision.SUSPECTED_DUPLICATE}
        for record, decision in decisions
    )
    review_count = sum(decision == Decision.SUSPECTED_DUPLICATE for _, decision in decisions)
    total = len(records)
    return {
        "pair_count": total,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "accuracy": (tp + tn) / total if total else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
        "candidate_recall": candidate_hits / duplicate_total if duplicate_total else 0.0,
        "review_rate": review_count / total if total else 0.0,
        "auto_coverage": (total - review_count) / total if total else 0.0,
    }


def calibrate_thresholds(
    records: list[ScoreRecord],
    *,
    minimum_evidence_coverage: float = 0.70,
) -> ThresholdProfile:
    dev = [record for record in records if record.split == DatasetSplit.DEV]
    if not dev:
        raise ValueError("DEV records are required for threshold calibration")
    grid = [index / 100 for index in range(50, 100)]
    duplicate_candidates: list[tuple[tuple[float, ...], float]] = []
    for duplicate in grid:
        profile = ThresholdProfile(
            suspected=min(0.75, duplicate),
            duplicate=duplicate,
            minimum_evidence_coverage=minimum_evidence_coverage,
        )
        metrics = classification_metrics(dev, profile)
        precision = float(metrics["precision"])
        recall = float(metrics["recall"])
        f1 = float(metrics["f1"])
        meets_precision = 1.0 if precision >= 0.90 else 0.0
        duplicate_candidates.append(((meets_precision, f1, recall, precision, -duplicate), duplicate))
    duplicate = max(duplicate_candidates, key=lambda item: item[0])[1]

    suspected_candidates: list[tuple[tuple[float, ...], float]] = []
    for suspected in [index / 100 for index in range(40, int(duplicate * 100) + 1)]:
        profile = ThresholdProfile(
            suspected=suspected,
            duplicate=duplicate,
            minimum_evidence_coverage=minimum_evidence_coverage,
        )
        metrics = classification_metrics(dev, profile)
        candidate_recall = float(metrics["candidate_recall"])
        review_rate = float(metrics["review_rate"])
        meets_candidate_target = 1.0 if candidate_recall >= 0.90 else 0.0
        suspected_candidates.append(
            ((meets_candidate_target, candidate_recall, -review_rate, suspected), suspected)
        )
    suspected = max(suspected_candidates, key=lambda item: item[0])[1]
    return ThresholdProfile(
        suspected=suspected,
        duplicate=duplicate,
        minimum_evidence_coverage=minimum_evidence_coverage,
    )


class EvaluationRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        assets_path: Path = PROJECT_ROOT / "data" / "assets.jsonl",
        pairs_path: Path = PROJECT_ROOT / "data" / "pairs.csv",
        report_dir: Path = PROJECT_ROOT / "reports" / "latest",
    ) -> None:
        self.settings = settings
        self.assets_path = Path(assets_path)
        self.pairs_path = Path(pairs_path)
        self.report_dir = Path(report_dir)
        self.service = Task1Service.create(settings)

    def _prewarm(self, assets: dict[str, AssetInput], pairs: list[EvaluationPair]) -> None:
        texts: list[str] = []
        used_ids = {pair.asset_a_id for pair in pairs} | {pair.asset_b_id for pair in pairs}
        for asset_id in sorted(used_ids):
            blocks = self.service.semantic.text_blocks(assets[asset_id])
            texts.extend(blocks[key] for key in SEMANTIC_COMPONENT_WEIGHTS if blocks[key].strip())
        self.service.semantic.embedding_client.embed_many(texts)

    def _score(
        self,
        assets: dict[str, AssetInput],
        pairs: list[EvaluationPair],
        mode: ComparisonMode,
    ) -> list[ScoreRecord]:
        records: list[ScoreRecord] = []
        for pair in pairs:
            left = assets[pair.asset_a_id]
            right = assets[pair.asset_b_id]
            started = time.perf_counter()
            if mode == ComparisonMode.KEYWORD_BASELINE:
                result = self.service.keyword.compare(left, right)
                if not result.available or result.score is None:
                    raise ValueError(f"keyword score unavailable: {pair.pair_id}")
                score = result.score
                coverage = result.quality
                semantic_score = logic_score = lineage_score = None
            else:
                semantic = self.service.semantic.compare(left, right)
                logic = self.service.logic.compare(left, right)
                lineage = self.service.lineage.compare(left, right)
                outcome = self.service.fusion.fuse(semantic, logic, lineage)
                score = outcome.score
                coverage = outcome.evidence_coverage
                semantic_score = semantic.score
                logic_score = logic.score
                lineage_score = lineage.score
            records.append(
                ScoreRecord(
                    pair_id=pair.pair_id,
                    family_id=pair.family_id,
                    split=pair.split,
                    scenario=pair.scenario,
                    label=pair.label,
                    mode=mode,
                    score=score,
                    evidence_coverage=coverage,
                    semantic_score=semantic_score,
                    logic_score=logic_score,
                    lineage_score=lineage_score,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            )
        return records

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _write_csv(path: Path, rows: list[dict]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8-sig")
            return
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def run(self) -> Path:
        assets = load_assets(self.assets_path)
        pairs = load_pairs(self.pairs_path)
        missing = sorted(
            ({pair.asset_a_id for pair in pairs} | {pair.asset_b_id for pair in pairs})
            - set(assets)
        )
        if missing:
            raise ValueError(f"pairs reference missing assets: {missing}")
        self._prewarm(assets, pairs)
        all_records: list[ScoreRecord] = []
        profiles: dict[ComparisonMode, ThresholdProfile] = {}
        summaries: list[dict] = []
        pair_rows: list[dict] = []
        for mode in (ComparisonMode.KEYWORD_BASELINE, ComparisonMode.THREE_LAYER):
            records = self._score(assets, pairs, mode)
            profile = calibrate_thresholds(
                records,
                minimum_evidence_coverage=self.settings.minimum_evidence_coverage,
            )
            profiles[mode] = profile
            all_records.extend(records)
            for split in (DatasetSplit.DEV, DatasetSplit.TEST):
                selected = [record for record in records if record.split == split]
                metrics = classification_metrics(selected, profile)
                summaries.append(
                    {
                        "mode": mode.value,
                        "split": split.value,
                        "suspected_threshold": profile.suspected,
                        "duplicate_threshold": profile.duplicate,
                        **metrics,
                    }
                )
            for record in records:
                decision = apply_threshold(record, profile)
                row = asdict(record)
                row["split"] = record.split.value
                row["label"] = record.label.value
                row["mode"] = record.mode.value
                row["decision"] = decision.value
                row["correct"] = (
                    decision == Decision.DUPLICATE
                    if record.label == GoldLabel.DUPLICATE
                    else decision == Decision.NOT_DUPLICATE
                )
                pair_rows.append(row)

        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._write_csv(self.report_dir / "summary.csv", summaries)
        self._write_csv(self.report_dir / "pair_results.csv", pair_rows)
        (self.report_dir / "thresholds.json").write_text(
            json.dumps(
                {mode.value: asdict(profile) for mode, profile in profiles.items()},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.settings.calibrated_thresholds_path.write_text(
            json.dumps(
                {mode.value: asdict(profile) for mode, profile in profiles.items()},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        embedding_stats = self.service.semantic.embedding_client.stats()
        manifest = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "algorithm_version": self.settings.algorithm_version,
            "dataset_sha256": self._sha256(self.assets_path),
            "pairs_sha256": self._sha256(self.pairs_path),
            "model": self.settings.embedding_model,
            "dimension": self.settings.embedding_dimension,
            "send_dimensions": self.settings.embedding_send_dimensions,
            "embedding_stats": embedding_stats,
            "data_boundary": "public-schema-driven semi-synthetic metadata; not production accuracy evidence",
            "threshold_policy": "calibrated on DEV only; TEST never used for threshold selection",
            "runtime_thresholds_path": str(self.settings.calibrated_thresholds_path),
        }
        (self.report_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._write_markdown_summary(summaries)
        return self.report_dir

    def _write_markdown_summary(self, summaries: list[dict]) -> None:
        test_rows = [row for row in summaries if row["split"] == DatasetSplit.TEST.value]
        lines = [
            "# Task1 关键词基线与三层模型对照结果",
            "",
            "> 数据为公开Schema驱动的半仿真资产，仅用于工程预评测。",
            "",
            "| 模式 | Accuracy | Precision | Recall | F1 | Candidate Recall | Review Rate |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in test_rows:
            lines.append(
                f"| {row['mode']} | {row['accuracy']:.3f} | {row['precision']:.3f} | "
                f"{row['recall']:.3f} | {row['f1']:.3f} | {row['candidate_recall']:.3f} | "
                f"{row['review_rate']:.3f} |"
            )
        lines.extend(
            [
                "",
                "Accuracy只把自动判为DUPLICATE或NOT_DUPLICATE且与标签一致的样本计为正确；",
                "SUSPECTED_DUPLICATE计入人工复核率，不计为自动判对。",
                "",
            ]
        )
        (self.report_dir / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
