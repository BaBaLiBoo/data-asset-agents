from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from sqlglot import parse_one

from app.config import PROJECT_ROOT
from app.models import (
    AssetInput,
    ColumnMetadata,
    DatasetSplit,
    EvaluationPair,
    GoldLabel,
    LineageInput,
    TableMetadata,
)


OUTPUT_DIR = PROJECT_ROOT / "data" / "evaluation_v3"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    code: str
    domain: str
    name: str
    source_table: str
    root_source: str
    entity_field: str
    entity_name: str
    date_field: str
    measure: str
    alternate_measure: str
    aggregate: str
    active_filter: str


@dataclass(frozen=True, slots=True)
class BusinessDefinition:
    metric: MetricSpec
    period: str
    scope: str
    date_field: str
    measure: str
    aggregate: str
    condition_override: str | None = None
    conflict_note: str | None = None


METRICS = (
    MetricSpec(
        "DEP_BAL",
        "deposit",
        "客户存款余额",
        "dwd_deposit_transaction",
        "m_savings_account",
        "client_id",
        "客户",
        "transaction_date",
        "running_balance",
        "amount",
        "SUM",
        "is_reversed = 0",
    ),
    MetricSpec(
        "DEP_FLOW",
        "deposit",
        "客户存款交易额",
        "dwd_deposit_transaction",
        "m_savings_account_transaction",
        "client_id",
        "客户",
        "transaction_date",
        "amount",
        "running_balance",
        "SUM",
        "is_reversed = 0",
    ),
    MetricSpec(
        "DEP_TXN_CNT",
        "deposit",
        "客户存款交易笔数",
        "dwd_deposit_transaction",
        "m_savings_account_transaction",
        "client_id",
        "客户",
        "transaction_date",
        "deposit_transaction_id",
        "amount",
        "COUNT_DISTINCT",
        "is_reversed = 0",
    ),
    MetricSpec(
        "DEP_ACCT_CNT",
        "deposit",
        "客户存款账户数",
        "dwd_deposit_account",
        "m_savings_account",
        "client_id",
        "客户",
        "activatedon_date",
        "deposit_account_id",
        "approved_amount",
        "COUNT_DISTINCT",
        "account_status = 'ACTIVE'",
    ),
    MetricSpec(
        "LOAN_OUT",
        "loan",
        "客户贷款未偿余额",
        "dwd_loan_account",
        "m_loan",
        "client_id",
        "客户",
        "disbursedon_date",
        "total_outstanding",
        "principal_amount",
        "SUM",
        "loan_status_id = 300",
    ),
    MetricSpec(
        "LOAN_DISB",
        "loan",
        "客户贷款放款金额",
        "dwd_loan_account",
        "m_loan",
        "client_id",
        "客户",
        "disbursedon_date",
        "principal_amount",
        "total_outstanding",
        "SUM",
        "loan_status_id IN (300, 600)",
    ),
    MetricSpec(
        "LOAN_REPAY",
        "loan",
        "客户贷款还款金额",
        "dwd_loan_transaction",
        "m_loan_transaction",
        "client_id",
        "客户",
        "transaction_date",
        "amount",
        "principal_portion",
        "SUM",
        "is_reversed = 0",
    ),
    MetricSpec(
        "LOAN_TXN_CNT",
        "loan",
        "客户贷款交易笔数",
        "dwd_loan_transaction",
        "m_loan_transaction",
        "client_id",
        "客户",
        "transaction_date",
        "loan_transaction_id",
        "amount",
        "COUNT_DISTINCT",
        "is_reversed = 0",
    ),
    MetricSpec(
        "GL_NET",
        "accounting",
        "机构会计净额",
        "dwd_gl_journal",
        "acc_gl_journal_entry",
        "office_id",
        "机构",
        "entry_date",
        "signed_amount",
        "amount",
        "SUM",
        "reversed = 0",
    ),
    MetricSpec(
        "GL_ENTRY_CNT",
        "accounting",
        "机构会计分录笔数",
        "dwd_gl_journal",
        "acc_gl_journal_entry",
        "office_id",
        "机构",
        "entry_date",
        "journal_entry_id",
        "amount",
        "COUNT_DISTINCT",
        "reversed = 0",
    ),
)

PERIOD_NAMES = {"day": "日", "month": "月", "quarter": "季度"}
SCOPE_NAMES = {"ALL": "全部", "ACTIVE": "有效", "CNY": "人民币"}


def stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def condition_for(definition: BusinessDefinition) -> str:
    if definition.condition_override is not None:
        return definition.condition_override
    if definition.scope == "ALL":
        return "1 = 1"
    if definition.scope == "ACTIVE":
        return definition.metric.active_filter
    if definition.scope == "CNY":
        return "currency_code = 'CNY'"
    raise ValueError(f"unknown scope: {definition.scope}")


def aggregate_expression(definition: BusinessDefinition) -> str:
    if definition.aggregate == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {definition.measure})"
    if definition.aggregate == "COUNT_ROWS":
        return "COUNT(*)"
    return f"{definition.aggregate}({definition.measure})"


def definition_name(definition: BusinessDefinition) -> str:
    return (
        f"{SCOPE_NAMES[definition.scope]}"
        f"{definition.metric.entity_name}"
        f"{PERIOD_NAMES[definition.period]}"
        f"{definition.metric.name.replace(definition.metric.entity_name, '', 1)}"
    )


def render_sql(definition: BusinessDefinition) -> str:
    period_expression = (
        f"DATE_TRUNC('{definition.period}', {definition.date_field})"
    )
    return (
        f"SELECT {definition.metric.entity_field}, "
        f"{period_expression} AS stat_period, currency_code, "
        f"{aggregate_expression(definition)} AS metric_value "
        f"FROM {definition.metric.source_table} "
        f"WHERE {condition_for(definition)} "
        f"GROUP BY {definition.metric.entity_field}, "
        f"{period_expression}, currency_code"
    )


def make_asset(
    asset_id: str,
    definition: BusinessDefinition,
    *,
    asset_name: str | None = None,
    description: str | None = None,
    sql_text: str | None = None,
    physical_name: str | None = None,
    lineage: LineageInput | None = None,
) -> AssetInput:
    name = asset_name or definition_name(definition)
    formula = aggregate_expression(definition)
    condition = condition_for(definition)
    note = f"；{definition.conflict_note}" if definition.conflict_note else ""
    business_description = description or (
        f"按{definition.metric.entity_name}、{PERIOD_NAMES[definition.period]}和币种，"
        f"计算{name}，过滤条件为{condition}{note}"
    )
    sql = sql_text or render_sql(definition)
    parse_one(sql, read="spark")
    output_table = physical_name or asset_id.lower()
    lineage_value = lineage or LineageInput(
        direct_upstreams=[definition.metric.source_table],
        root_sources=[definition.metric.root_source],
        column_signatures=[
            (
                f"{definition.metric.entity_field}"
                f"<-{definition.metric.source_table}.{definition.metric.entity_field}"
            ),
            (
                f"stat_period<-date_trunc({definition.period},"
                f"{definition.metric.source_table}.{definition.date_field})"
            ),
            f"currency_code<-{definition.metric.source_table}.currency_code",
            (
                f"metric_value<-{definition.aggregate.lower()}"
                f"({definition.metric.source_table}.{definition.measure})"
            ),
        ],
        coverage=0.92,
        freshness=0.95,
        source_reliability=0.90,
    )
    return AssetInput(
        asset_id=asset_id,
        asset_name=name,
        description=business_description,
        business_domain=definition.metric.domain,
        sql_text=sql,
        sql_dialect="spark",
        declared_grain=[
            definition.metric.entity_field,
            definition.period,
            "currency_code",
        ],
        metric_name=name,
        metric_definition=(
            f"{formula}；时间字段={definition.date_field}；"
            f"过滤条件={condition}{note}"
        ),
        tables=[
            TableMetadata(
                name=output_table,
                cn_name=name,
                comment=business_description,
                columns=[
                    ColumnMetadata(
                        name=definition.metric.entity_field,
                        cn_name=f"{definition.metric.entity_name}编号",
                        role="dimension",
                        data_type="BIGINT",
                    ),
                    ColumnMetadata(
                        name="stat_period",
                        cn_name="统计周期",
                        role="date",
                        data_type="DATE",
                    ),
                    ColumnMetadata(
                        name="currency_code",
                        cn_name="币种",
                        role="dimension",
                        data_type="STRING",
                    ),
                    ColumnMetadata(
                        name="metric_value",
                        cn_name=name,
                        role="measure",
                        data_type="DECIMAL",
                    ),
                ],
            )
        ],
        lineage=lineage_value,
    )


def synonym_name(value: str) -> str:
    replacements = (
        ("客户", "客户对象"),
        ("存款", "储蓄"),
        ("贷款", "信贷"),
        ("会计", "财务核算"),
        ("余额", "结余"),
        ("金额", "发生额"),
        ("笔数", "次数"),
        ("机构", "营业机构"),
    )
    changed = value
    for source, target in replacements:
        if source in changed:
            changed = changed.replace(source, target, 1)
    return changed if changed != value else f"{value}等价版本"


def duplicate_variant(
    base: AssetInput,
    definition: BusinessDefinition,
    split: DatasetSplit,
) -> tuple[AssetInput, str]:
    asset_id = f"{base.asset_id}_DUP"
    if split == DatasetSplit.DEV:
        sql = f"WITH source_asset AS ({base.sql_text}) SELECT * FROM source_asset"
        scenario = "DUPLICATE_CTE_REWRITE"
    else:
        sql = f"SELECT * FROM ({base.sql_text}) source_asset WHERE 1 = 1"
        scenario = "DUPLICATE_SUBQUERY_REWRITE"
    return (
        make_asset(
            asset_id,
            definition,
            asset_name=synonym_name(base.asset_name),
            description=synonym_name(base.description),
            sql_text=sql,
            physical_name=asset_id.lower(),
            lineage=base.lineage.model_copy(deep=True),
        ),
        scenario,
    )


def hard_definition(
    definition: BusinessDefinition,
    *,
    split: DatasetSplit,
    index: int,
) -> tuple[BusinessDefinition, str]:
    if split == DatasetSplit.DEV:
        conflict = ("AGGREGATION", "FILTER")[index % 2]
    else:
        conflict = ("TIME_GRAIN", "DATE_FIELD", "MEASURE", "CURRENCY_SCOPE")[
            index % 4
        ]
    if conflict == "AGGREGATION":
        aggregate = (
            "COUNT_ROWS"
            if definition.aggregate == "COUNT_DISTINCT"
            else "AVG"
        )
        changed = replace(
            definition,
            aggregate=aggregate,
            conflict_note="聚合方式与基础资产不同",
        )
    elif conflict == "FILTER":
        scope = {"ALL": "ACTIVE", "ACTIVE": "CNY", "CNY": "ALL"}[
            definition.scope
        ]
        changed = replace(
            definition,
            scope=scope,
            conflict_note="核心过滤范围与基础资产不同",
        )
    elif conflict == "TIME_GRAIN":
        period = {"day": "month", "month": "quarter", "quarter": "day"}[
            definition.period
        ]
        changed = replace(
            definition,
            period=period,
            conflict_note="统计时间粒度与基础资产不同",
        )
    elif conflict == "DATE_FIELD":
        changed = replace(
            definition,
            date_field="posting_date",
            conflict_note="统计日期口径与基础资产不同",
        )
    elif conflict == "MEASURE":
        changed = replace(
            definition,
            measure=definition.metric.alternate_measure,
            conflict_note="核心度量字段与基础资产不同",
        )
    else:
        condition = (
            "currency_code = 'USD'"
            if definition.scope == "CNY"
            else f"({condition_for(definition)}) AND currency_code = 'CNY'"
        )
        changed = replace(
            definition,
            condition_override=condition,
            conflict_note="币种统计范围与基础资产不同",
        )
    return changed, f"HARD_{conflict}_CONFLICT"


def distractor_asset(
    seed: AssetInput,
    definition: BusinessDefinition,
    domain_index: int,
) -> AssetInput:
    condition = (
        f"({condition_for(definition)}) AND "
        f"office_id = {(domain_index % 20) + 1}"
    )
    changed = replace(
        definition,
        condition_override=condition,
        conflict_note="限定特定机构的专项分析口径",
    )
    asset_id = f"DIST_{definition.metric.domain.upper()}_{domain_index:03d}"
    return make_asset(
        asset_id,
        changed,
        asset_name=f"{seed.asset_name}专项分析{domain_index + 1}",
        physical_name=asset_id.lower(),
    )


def build_base_assets() -> tuple[list[AssetInput], dict[str, BusinessDefinition]]:
    assets: list[AssetInput] = []
    definitions: dict[str, BusinessDefinition] = {}
    for metric in METRICS:
        for period in ("day", "month", "quarter"):
            for scope in ("ALL", "ACTIVE", "CNY"):
                definition = BusinessDefinition(
                    metric=metric,
                    period=period,
                    scope=scope,
                    date_field=metric.date_field,
                    measure=metric.measure,
                    aggregate=metric.aggregate,
                )
                asset_id = f"IND_{metric.code}_{period.upper()}_{scope}"
                asset = make_asset(asset_id, definition)
                assets.append(asset)
                definitions[asset_id] = definition
    return assets, definitions


def build_distractors(
    bases: list[AssetInput],
    definitions: dict[str, BusinessDefinition],
) -> dict[str, list[AssetInput]]:
    by_domain = {
        domain: [asset for asset in bases if asset.business_domain == domain]
        for domain in ("deposit", "loan", "accounting")
    }
    result: dict[str, list[AssetInput]] = {}
    for domain, seeds in by_domain.items():
        result[domain] = [
            distractor_asset(
                seeds[index % len(seeds)],
                definitions[seeds[index % len(seeds)].asset_id],
                index,
            )
            for index in range(30)
        ]
    return result


def clear_pair_assignment(
    bases: list[AssetInput],
    distractors: dict[str, list[AssetInput]],
) -> dict[str, AssetInput]:
    base_by_domain = {
        domain: [asset for asset in bases if asset.business_domain == domain]
        for domain in ("deposit", "loan", "accounting")
    }
    mapping: dict[str, AssetInput] = {}
    for index, base in enumerate(base_by_domain["deposit"]):
        target_domain = "loan" if index < 21 else "accounting"
        target_index = index if index < 21 else index - 21
        mapping[base.asset_id] = distractors[target_domain][target_index]
    for index, base in enumerate(base_by_domain["loan"]):
        target_domain = "deposit" if index < 21 else "accounting"
        target_index = index if index < 21 else index - 6
        mapping[base.asset_id] = distractors[target_domain][target_index]
    for index, base in enumerate(base_by_domain["accounting"]):
        target_domain = "deposit" if index < 9 else "loan"
        target_index = index + 21 if index < 9 else index + 12
        mapping[base.asset_id] = distractors[target_domain][target_index]
    assigned = list(mapping.values())
    if len(assigned) != len(bases) or len({item.asset_id for item in assigned}) != len(bases):
        raise ValueError("clear-negative distractor assignment must be one-to-one")
    if any(
        base.business_domain == mapping[base.asset_id].business_domain
        for base in bases
    ):
        raise ValueError("clear-negative pairs must be cross-domain")
    return mapping


def generate(output_dir: Path = OUTPUT_DIR) -> dict[str, object]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    bases, definitions = build_base_assets()
    ordered_ids = sorted(
        (asset.asset_id for asset in bases),
        key=stable_digest,
    )
    dev_ids = set(ordered_ids[:30])
    split_by_id = {
        asset.asset_id: (
            DatasetSplit.DEV
            if asset.asset_id in dev_ids
            else DatasetSplit.TEST
        )
        for asset in bases
    }
    distractors = build_distractors(bases, definitions)
    clear_targets = clear_pair_assignment(bases, distractors)

    variants: list[AssetInput] = []
    pairs: list[EvaluationPair] = []

    def add_pair(
        base: AssetInput,
        other: AssetInput,
        label: GoldLabel,
        scenario: str,
    ) -> None:
        pairs.append(
            EvaluationPair(
                pair_id=f"V3-{len(pairs) + 1:03d}",
                family_id=f"V3-FAMILY-{base.asset_id}",
                asset_a_id=base.asset_id,
                asset_b_id=other.asset_id,
                label=label,
                scenario=scenario,
                split=split_by_id[base.asset_id],
            )
        )

    for index, base in enumerate(bases):
        definition = definitions[base.asset_id]
        split = split_by_id[base.asset_id]
        duplicate, duplicate_scenario = duplicate_variant(base, definition, split)
        hard_spec, hard_scenario = hard_definition(
            definition,
            split=split,
            index=index,
        )
        hard = make_asset(
            f"{base.asset_id}_HARD",
            hard_spec,
            asset_name=f"{base.asset_name}另一口径",
        )
        variants.extend([duplicate, hard])
        add_pair(base, duplicate, GoldLabel.DUPLICATE, duplicate_scenario)
        add_pair(base, hard, GoldLabel.NOT_DUPLICATE, hard_scenario)
        add_pair(
            base,
            clear_targets[base.asset_id],
            GoldLabel.NOT_DUPLICATE,
            "CLEAR_CROSS_DOMAIN",
        )

    all_distractors = [
        asset
        for domain in ("deposit", "loan", "accounting")
        for asset in distractors[domain]
    ]
    all_assets = [*bases, *variants, *all_distractors]
    if len(all_assets) != 360 or len(pairs) != 270:
        raise ValueError("independent dataset must contain 360 assets and 270 pairs")
    if len({asset.asset_id for asset in all_assets}) != len(all_assets):
        raise ValueError("independent dataset contains duplicate asset_id")

    asset_splits: dict[str, DatasetSplit] = {}
    for pair in pairs:
        for asset_id in (pair.asset_a_id, pair.asset_b_id):
            previous = asset_splits.setdefault(asset_id, pair.split)
            if previous != pair.split:
                raise ValueError(f"asset split leakage: {asset_id}")
    family_splits: dict[str, DatasetSplit] = {}
    for pair in pairs:
        previous = family_splits.setdefault(pair.family_id, pair.split)
        if previous != pair.split:
            raise ValueError(f"family split leakage: {pair.family_id}")

    assets_path = output_dir / "assets.jsonl"
    pairs_path = output_dir / "pairs.csv"
    assets_path.write_text(
        "\n".join(asset.model_dump_json() for asset in all_assets) + "\n",
        encoding="utf-8",
    )
    with pairs_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "pair_id",
                "family_id",
                "asset_a_id",
                "asset_b_id",
                "label",
                "scenario",
                "split",
            ],
        )
        writer.writeheader()
        for pair in pairs:
            writer.writerow(pair.model_dump(mode="json"))

    label_counts = Counter(pair.label.value for pair in pairs)
    split_counts = Counter(pair.split.value for pair in pairs)
    scenario_groups = Counter(
        "duplicate"
        if pair.label == GoldLabel.DUPLICATE
        else (
            "hard_not_duplicate"
            if pair.scenario.startswith("HARD_")
            else "clear_not_duplicate"
        )
        for pair in pairs
    )
    manifest = {
        "version": "independent-evaluation-v3.0",
        "asset_count": len(all_assets),
        "pair_count": len(pairs),
        "base_asset_count": len(bases),
        "duplicate_variant_count": 90,
        "hard_variant_count": 90,
        "same_domain_distractor_count": 90,
        "label_distribution": dict(label_counts),
        "scenario_distribution": dict(scenario_groups),
        "split_distribution": dict(split_counts),
        "dev_family_count": 30,
        "test_family_count": 60,
        "asset_split_leakage": 0,
        "family_split_leakage": 0,
        "dev_duplicate_templates": ["CTE_REWRITE"],
        "test_duplicate_templates": ["SUBQUERY_REWRITE"],
        "dev_hard_conflicts": ["AGGREGATION", "FILTER"],
        "test_hard_conflicts": [
            "TIME_GRAIN",
            "DATE_FIELD",
            "MEASURE",
            "CURRENCY_SCOPE",
        ],
        "generation_independence": (
            "generator uses business specifications and deterministic transformations; "
            "it does not import or query similarity scores, fusion weights, or thresholds"
        ),
        "boundary": (
            "public-schema-inspired semi-synthetic data; labels are deterministic "
            "business-spec labels, not bank-expert blind annotations"
        ),
        "assets_sha256": hashlib.sha256(assets_path.read_bytes()).hexdigest(),
        "pairs_sha256": hashlib.sha256(pairs_path.read_bytes()).hexdigest(),
    }
    (output_dir / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    manifest = generate()
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
