from __future__ import annotations

import hashlib
import json
from pathlib import Path

import sqlglot

from data_asset_agents.core.errors import DataAssetAgentsError
from data_asset_agents.evaluation.models import BenchmarkSuite
from data_asset_agents.execution.protocols import ExecutorProtocol
from data_asset_agents.validation import (
    CommonSQLSafetyValidator,
    OntologyPolicyValidator,
)


def load_benchmark(path: Path | str) -> BenchmarkSuite:
    source = Path(path)
    if not source.exists():
        raise DataAssetAgentsError(f"Benchmark does not exist: {source}")
    return BenchmarkSuite.model_validate(json.loads(source.read_text(encoding="utf-8")))


def benchmark_source_hash(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_benchmark(
    suite: BenchmarkSuite,
    common: CommonSQLSafetyValidator,
    ontology_policy: OntologyPolicyValidator,
    executor: ExecutorProtocol | None = None,
) -> list[str]:
    """Validate human/DSL-authored Gold SQL without invoking a tested strategy."""

    errors: list[str] = []
    for case in suite.cases:
        if case.expected_status != "success" or case.gold_sql is None:
            continue
        common_report = common.validate(case.gold_sql, set(case.gold_tables))
        if not common_report.valid:
            errors.append(f"{case.id}: common: {'; '.join(common_report.errors)}")
            continue
        policy_report = ontology_policy.validate(case.gold_sql)
        if not policy_report.valid:
            errors.append(f"{case.id}: ontology: {'; '.join(policy_report.errors)}")
            continue
        try:
            sqlglot.parse_one(case.gold_sql, read="postgres")
            if executor is not None:
                executor.explain(case.gold_sql, set(case.gold_tables))
        except Exception as exc:
            errors.append(f"{case.id}: database: {exc}")
    return errors
