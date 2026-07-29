from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from sqlglot import parse_one
from sqlglot.errors import ParseError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "data" / "evaluation_v3"
OUTPUT_DIR = PROJECT_ROOT / "data" / "retrieval_evaluation"


@dataclass(frozen=True, slots=True)
class Family:
    family_id: str
    split: str
    base_id: str
    duplicate_id: str


def load_assets(path: Path) -> dict[str, dict]:
    assets: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payload = json.loads(line)
            assets[payload["asset_id"]] = payload
    return assets


def load_families(path: Path) -> list[Family]:
    families: list[Family] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["label"] != "DUPLICATE":
                continue
            families.append(
                Family(
                    family_id=row["family_id"],
                    split=row["split"],
                    base_id=row["asset_a_id"],
                    duplicate_id=row["asset_b_id"],
                )
            )
    return sorted(families, key=lambda item: item.family_id)


def synonym_rewrite(text: str) -> str:
    replacements = (
        ("贷款", "信贷"),
        ("存款", "储蓄"),
        ("客户", "客群"),
        ("交易额", "发生金额"),
        ("交易笔数", "发生次数"),
        ("余额", "结余"),
        ("机构", "网点"),
        ("日", "按日"),
        ("月", "按月"),
        ("季度", "按季度"),
    )
    rewritten = text
    for source, target in replacements:
        if source in rewritten:
            rewritten = rewritten.replace(source, target, 1)
            break
    return rewritten


def build_text_query(asset: dict, index: int) -> tuple[str, str]:
    name = asset["asset_name"]
    metric = asset.get("metric_name") or name
    description = asset.get("description") or metric
    grain = "、".join(asset.get("declared_grain") or [])
    slot = index % 20
    if slot < 4:
        return name, "STANDARD"
    if slot < 8:
        return f"查询{synonym_rewrite(metric)}相关数据", "SYNONYM"
    if slot < 12:
        return f"手头要做{metric}，库里有能直接拿来用的吗", "COLLOQUIAL"
    if slot < 15:
        return f"查找与{description}口径接近但需要核对的资产", "NEAR_DISTRACTOR"
    if slot < 17:
        broad = re.sub(r"(全部|有效|人民币|逾期|正常)", "", metric)
        return f"有没有{broad}这类指标", "AMBIGUOUS"
    detail = f"，粒度为{grain}" if grain else ""
    return f"需要一份{description}{detail}", "METRIC_PARAPHRASE"


def equivalent_sql(sql_text: str, dialect: str, variant: int) -> str:
    sql = sql_text.strip().rstrip(";")
    if variant % 3 == 0:
        return f"WITH source_asset AS ({sql}) SELECT * FROM source_asset"
    try:
        root = parse_one(sql, read=dialect)
    except (ParseError, ValueError):
        return sql
    select = root if root.__class__.__name__ == "Select" else root.find(
        type(parse_one("SELECT 1"))
    )
    if variant % 3 == 1 and select is not None and len(select.expressions) > 1:
        select.set("expressions", list(reversed(select.expressions)))
    return root.sql(dialect=dialect, pretty=variant % 3 == 2)


NO_MATCH_QUERIES = (
    "查询股票期权组合的希腊字母敞口",
    "寻找信用卡营销短信点击转化率",
    "统计保险保单年度退保率",
    "计算黄金衍生品逐笔盯市损益",
    "查询跨境贸易融资国家风险限额",
    "统计手机银行页面停留时长中位数",
    "寻找ATM设备预测性维修指标",
    "分析员工培训课程完成率",
    "统计客户投诉录音情绪波动",
    "查询数据中心服务器能耗强度",
    "计算债券组合久期和凸性",
    "寻找供应链票据逾期迁徙率",
    "统计反洗钱可疑报告退回率",
    "查询网银登录设备指纹风险",
    "计算基金定投客户留存率",
    "寻找外汇做市报价点差",
    "统计监管报送字段驳回次数",
    "查询智能客服意图识别准确率",
)

NO_MATCH_SQL = (
    "SELECT portfolio_id, SUM(delta_value) AS metric_value "
    "FROM dwd_option_position GROUP BY portfolio_id",
    "SELECT device_id, AVG(power_kwh) AS metric_value "
    "FROM dwd_server_energy GROUP BY device_id",
    "SELECT course_id, COUNT(DISTINCT employee_id) AS metric_value "
    "FROM dwd_training_completion GROUP BY course_id",
)


def generate() -> list[dict]:
    assets = load_assets(SOURCE_DIR / "assets.jsonl")
    families = load_families(SOURCE_DIR / "pairs.csv")
    rows: list[dict] = []
    sequence = 1
    for index, family in enumerate(families):
        asset = assets[family.base_id]
        query, query_type = build_text_query(asset, index)
        base = {
            "family_id": family.family_id,
            "business_domain": asset["business_domain"],
            "expected_asset_ids": [family.base_id, family.duplicate_id],
            "has_match": True,
            "split": family.split,
            "source_asset_id": family.base_id,
        }
        rows.append(
            {
                "query_id": f"RQ-{sequence:04d}",
                "query": query,
                "sql_text": None,
                "sql_dialect": asset.get("sql_dialect", "spark"),
                "query_type": query_type,
                **base,
            }
        )
        sequence += 1

        if index % 3 == 0:
            rows.append(
                {
                    "query_id": f"RQ-{sequence:04d}",
                    "query": f"想复用{asset.get('metric_name') or asset['asset_name']}的加工逻辑",
                    "sql_text": equivalent_sql(
                        asset["sql_text"],
                        asset.get("sql_dialect", "spark"),
                        index,
                    ),
                    "sql_dialect": asset.get("sql_dialect", "spark"),
                    "query_type": "TEXT_SQL",
                    **base,
                }
            )
            sequence += 1

    dev_negative_count = max(
        1,
        round(sum(row["split"] == "DEV" for row in rows) * 0.15),
    )
    test_negative_count = max(
        1,
        round(sum(row["split"] == "TEST" for row in rows) * 0.15),
    )
    for split, count, offset in (
        ("DEV", dev_negative_count, 0),
        ("TEST", test_negative_count, dev_negative_count),
    ):
        for index in range(count):
            query = NO_MATCH_QUERIES[(offset + index) % len(NO_MATCH_QUERIES)]
            sql_text = None
            query_type = "NO_MATCH"
            if index % 5 == 0:
                sql_text = "SELECT FROM WHERE"
                query_type = "NO_MATCH_SQL_INVALID"
            elif index % 4 == 0:
                sql_text = NO_MATCH_SQL[index % len(NO_MATCH_SQL)]
                query_type = "NO_MATCH_SQL"
            rows.append(
                {
                    "query_id": f"RQ-{sequence:04d}",
                    "query": query,
                    "sql_text": sql_text,
                    "sql_dialect": "spark",
                    "business_domain": None,
                    "expected_asset_ids": [],
                    "has_match": False,
                    "query_type": query_type,
                    "split": split,
                    "family_id": f"NO_MATCH-{split}-{index + 1:03d}",
                    "source_asset_id": None,
                }
            )
            sequence += 1
    return rows


def validate(rows: list[dict]) -> None:
    ids = [row["query_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate query_id")
    dev_families = {
        row["family_id"]
        for row in rows
        if row["split"] == "DEV" and row["has_match"]
    }
    test_families = {
        row["family_id"]
        for row in rows
        if row["split"] == "TEST" and row["has_match"]
    }
    overlap = dev_families & test_families
    if overlap:
        raise ValueError(f"DEV/TEST family leakage: {sorted(overlap)[:3]}")
    for row in rows:
        if row["has_match"] != bool(row["expected_asset_ids"]):
            raise ValueError(f"invalid expected ids: {row['query_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_DIR / "queries.jsonl",
    )
    args = parser.parse_args()
    rows = generate()
    validate(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    summary = {
        "query_count": len(rows),
        "dev_count": sum(row["split"] == "DEV" for row in rows),
        "test_count": sum(row["split"] == "TEST" for row in rows),
        "sql_count": sum(bool(row["sql_text"]) for row in rows),
        "no_match_count": sum(not row["has_match"] for row in rows),
        "source": "data/evaluation_v3",
        "limitations": "半仿真工程评测，不能替代真实用户检索评测",
    }
    (args.output.parent / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
