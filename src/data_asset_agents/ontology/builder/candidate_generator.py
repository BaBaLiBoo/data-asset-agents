from __future__ import annotations

import json
import re
from collections import Counter

from data_asset_agents.core.config import Settings
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.models import (
    CandidateConcept,
    CandidateJoin,
    CandidateMapping,
    CandidateSemanticOutput,
    ColumnProfile,
    HistoricalSQLAnalysis,
    HistoricalSQLSummary,
    MetadataSnapshot,
)

KNOWN_COLUMNS: dict[str, tuple[str, str, str, str, list[str]]] = {
    "customer_id": ("客户编号", "客户", "客户唯一标识", "identifier", ["客户ID"]),
    "account_id": ("账户编号", "账户", "账户唯一标识", "identifier", ["账户ID"]),
    "card_id": ("银行卡编号", "银行卡", "银行卡唯一标识", "identifier", ["卡号标识"]),
    "transaction_id": ("交易编号", "交易", "交易唯一标识", "identifier", ["流水号"]),
    "branch_id": ("分行编号", "分行", "分行唯一标识", "identifier", ["机构编号"]),
    "branch_name": ("分行", "分行", "分行名称", "dimension", ["机构", "网点"]),
    "merchant_id": ("商户编号", "商户", "商户唯一标识", "identifier", ["商户ID"]),
    "merchant_category": ("商户类别", "商户", "商户经营类别", "dimension", ["商户类型"]),
    "customer_type": ("客户类型", "客户", "客户分层类型", "dimension", ["客户类别"]),
    "card_type": ("卡类型", "银行卡", "借记卡或贷记卡类型", "dimension", ["银行卡类型"]),
    "transaction_channel": ("交易渠道", "交易", "交易发生渠道", "dimension", ["渠道"]),
    "transaction_status": ("交易状态", "交易", "交易处理状态", "status", ["流水状态"]),
    "transaction_date": ("交易日期", "交易", "交易发生日期", "time", ["交易日"]),
    "transaction_month": ("交易月份", "交易", "交易统计月份", "time", ["月份"]),
    "txn_amount_cny": ("人民币交易金额", "交易", "人民币交易金额", "measure", ["交易额"]),
    "txn_amount": ("原始交易金额", "交易", "交易原币金额", "measure", ["原始金额"]),
    "posted_amount": ("入账金额", "交易", "已入账金额", "measure", ["记账金额"]),
}
PUBLISHED_CONCEPT_IDS = {
    "branch_name": "dimension:branch",
    "card_type": "dimension:card_type",
    "transaction_channel": "dimension:transaction_channel",
    "transaction_date": "dimension:transaction_date",
    "customer_type": "dimension:customer_type",
    "merchant_category": "dimension:merchant_category",
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


class CandidateGenerator:
    """Generate review-only semantic candidates from masked physical evidence."""

    def __init__(self, settings: Settings, factory: ModelFactory | None = None) -> None:
        self.settings = settings
        self.factory = factory or ModelFactory(settings)

    def generate(
        self,
        snapshot: MetadataSnapshot,
        historical_sql: list[HistoricalSQLAnalysis],
        summary: HistoricalSQLSummary,
    ) -> tuple[list[CandidateConcept], list[CandidateMapping], list[CandidateJoin]]:
        concepts: list[CandidateConcept] = []
        mappings: list[CandidateMapping] = []
        for table in snapshot.tables:
            context_columns = [column.name for column in table.columns]
            for profile in table.profiles:
                output = self._candidate_output(
                    profile,
                    table.comment,
                    context_columns,
                    historical_sql,
                    summary,
                )
                semantic_id = PUBLISHED_CONCEPT_IDS.get(
                    profile.column_name,
                    f"attribute:{_slug(profile.table_name)}.{_slug(profile.column_name)}",
                )
                concept_id = (
                    f"concept_{snapshot.id}_{profile.table_name}_{profile.column_name}"
                )
                concept = CandidateConcept(
                    id=concept_id,
                    snapshot_id=snapshot.id,
                    table_name=profile.table_name,
                    column_name=profile.column_name,
                    semantic_id=semantic_id,
                    created_at=snapshot.captured_at,
                    **output.model_dump(),
                )
                concepts.append(concept)
                mappings.append(
                    CandidateMapping(
                        id=(
                            f"mapping_{snapshot.id}_{profile.table_name}_"
                            f"{profile.column_name}"
                        ),
                        snapshot_id=snapshot.id,
                        candidate_concept_id=concept.id,
                        concept_id=semantic_id,
                        table_name=profile.table_name,
                        columns=[profile.column_name],
                        column_bindings={"value": profile.column_name},
                        confidence=output.confidence,
                        evidence=[
                            f"来源字段 {profile.table_name}.{profile.column_name}",
                            *output.evidence,
                        ],
                        created_at=snapshot.captured_at,
                    )
                )
        joins = self._join_candidates(snapshot, historical_sql)
        return concepts, mappings, joins

    def _candidate_output(
        self,
        profile: ColumnProfile,
        table_comment: str | None,
        context_columns: list[str],
        historical_sql: list[HistoricalSQLAnalysis],
        summary: HistoricalSQLSummary,
    ) -> CandidateSemanticOutput:
        if self.settings.llm_mode == "mock":
            return self._mock_output(profile, historical_sql)
        prompt = self._prompt(
            profile, table_comment, context_columns, historical_sql, summary
        )
        model = self.factory.chat_model().with_structured_output(
            CandidateSemanticOutput
        )
        result = model.invoke(prompt)
        return CandidateSemanticOutput.model_validate(result)

    @staticmethod
    def _mock_output(
        profile: ColumnProfile,
        historical_sql: list[HistoricalSQLAnalysis],
    ) -> CandidateSemanticOutput:
        known = KNOWN_COLUMNS.get(profile.column_name)
        if known:
            name, business_object, semantic_property, role, synonyms = known
        else:
            lowered = profile.column_name.lower()
            if lowered.endswith("_id"):
                role = "identifier"
            elif any(token in lowered for token in ("date", "time", "month")):
                role = "time"
            elif any(token in lowered for token in ("amount", "count", "balance")):
                role = "measure"
            elif "status" in lowered:
                role = "status"
            elif any(token in lowered for token in ("type", "category", "channel")):
                role = "dimension"
            else:
                role = "attribute"
            name = profile.column_name.replace("_", " ").title()
            business_object = profile.table_name.replace("dim_", "").replace(
                "dwd_", ""
            )
            semantic_property = f"{business_object} 的 {name}"
            synonyms = []
        unit = "CNY" if "amount_cny" in profile.column_name.lower() else None
        sql_hits = sum(
            1
            for analysis in historical_sql
            for column in analysis.columns
            if column.column == profile.column_name
            and column.table in (None, profile.table_name)
        )
        evidence = [
            f"字段类型 {profile.data_type}",
            f"唯一率 {profile.unique_rate:.4f}，空值率 {profile.null_rate:.4f}",
            f"历史 SQL 引用 {sql_hits} 次",
        ]
        if profile.sample_values:
            evidence.append(f"脱敏样例：{', '.join(profile.sample_values[:3])}")
        confidence = 0.96 if known else 0.72
        return CandidateSemanticOutput(
            business_name=name,
            business_object=business_object,
            semantic_property=semantic_property,
            role=role,  # type: ignore[arg-type]
            unit=unit,
            synonyms=synonyms,
            confidence=confidence,
            evidence=evidence,
        )

    @staticmethod
    def _prompt(
        profile: ColumnProfile,
        table_comment: str | None,
        context_columns: list[str],
        historical_sql: list[HistoricalSQLAnalysis],
        summary: HistoricalSQLSummary,
    ) -> str:
        relevant = [
            analysis.model_dump(mode="json")
            for analysis in historical_sql
            if any(
                column.column == profile.column_name
                and column.table in (None, profile.table_name)
                for column in analysis.columns
            )
        ][:5]
        payload = {
            "table": profile.table_name,
            "table_comment": table_comment,
            "column_profile": profile.model_dump(mode="json"),
            "context_columns": context_columns,
            "historical_sql_evidence": relevant,
            "usage_summary": summary.model_dump(mode="json"),
        }
        return (
            "你是企业语义建模助手。仅依据以下脱敏证据生成一个候选语义，"
            "不得虚构业务规则。输出将进入人工审核，不能直接发布。\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    @staticmethod
    def _join_candidates(
        snapshot: MetadataSnapshot,
        historical_sql: list[HistoricalSQLAnalysis],
    ) -> list[CandidateJoin]:
        history_counts: Counter[tuple[str, str, str, str]] = Counter()
        for analysis in historical_sql:
            for join in analysis.joins:
                history_counts[
                    (
                        join.left_table,
                        join.left_column,
                        join.right_table,
                        join.right_column,
                    )
                ] += 1
        candidates: dict[frozenset[tuple[str, str]], CandidateJoin] = {}
        for table in snapshot.tables:
            for foreign_key in table.foreign_keys:
                for left_column, right_column in zip(
                    foreign_key.constrained_columns,
                    foreign_key.referred_columns,
                    strict=True,
                ):
                    key = frozenset(
                        {
                            (table.table_name, left_column),
                            (foreign_key.referred_table, right_column),
                        }
                    )
                    history_count = history_counts.get(
                        (
                            table.table_name,
                            left_column,
                            foreign_key.referred_table,
                            right_column,
                        ),
                        0,
                    ) + history_counts.get(
                        (
                            foreign_key.referred_table,
                            right_column,
                            table.table_name,
                            left_column,
                        ),
                        0,
                    )
                    candidates[key] = CandidateJoin(
                        id=(
                            f"join_{snapshot.id}_{table.table_name}_{left_column}_"
                            f"{foreign_key.referred_table}_{right_column}"
                        ),
                        snapshot_id=snapshot.id,
                        left_table=table.table_name,
                        left_column=left_column,
                        right_table=foreign_key.referred_table,
                        right_column=right_column,
                        relationship="many_to_one",
                        confidence=0.99,
                        evidence=[
                            f"数据库外键 {foreign_key.name or 'unnamed'}",
                            f"历史 SQL Join 使用 {history_count} 次",
                        ],
                        created_at=snapshot.captured_at,
                    )
        for analysis in historical_sql:
            for join in analysis.joins:
                key = frozenset(
                    {
                        (join.left_table, join.left_column),
                        (join.right_table, join.right_column),
                    }
                )
                if key not in candidates:
                    count = history_counts[
                        (
                            join.left_table,
                            join.left_column,
                            join.right_table,
                            join.right_column,
                        )
                    ]
                    candidates[key] = CandidateJoin(
                        id=(
                            f"join_{snapshot.id}_{join.left_table}_{join.left_column}_"
                            f"{join.right_table}_{join.right_column}"
                        ),
                        snapshot_id=snapshot.id,
                        left_table=join.left_table,
                        left_column=join.left_column,
                        right_table=join.right_table,
                        right_column=join.right_column,
                        confidence=min(0.75 + count * 0.05, 0.95),
                        evidence=[f"历史 SQL Join 使用 {count} 次"],
                        created_at=snapshot.captured_at,
                    )
        return sorted(candidates.values(), key=lambda item: item.id)
