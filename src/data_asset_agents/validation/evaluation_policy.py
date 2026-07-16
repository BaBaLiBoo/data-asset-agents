from __future__ import annotations

from pydantic import BaseModel, Field

from data_asset_agents.ontology.models import OntologyBundle
from data_asset_agents.text2sql.models import QueryFilter, SemanticQuery
from data_asset_agents.validation.ontology_policy import OntologyPolicyValidator


class EvaluationPolicyReport(BaseModel):
    valid: bool
    violations: list[str] = Field(default_factory=list)
    lifecycle_violations: list[str] = Field(default_factory=list)
    join_violations: list[str] = Field(default_factory=list)
    business_policy_violations: list[str] = Field(default_factory=list)


class EvaluationPolicyInspector:
    """Hidden, read-only scorer. It never rewrites SQL or feeds policy back to a model."""

    def __init__(self, ontology: OntologyBundle, version_id: str | None = None) -> None:
        self._validator = OntologyPolicyValidator(ontology, version_id)

    def inspect(
        self,
        generated_sql: str,
        *,
        semantic_query: SemanticQuery | None = None,
        required_filters: list[QueryFilter] | None = None,
    ) -> EvaluationPolicyReport:
        report = self._validator.validate(
            generated_sql,
            semantic_query=semantic_query,
            required_filters=required_filters or [],
        )
        lifecycle_codes = {"FORBIDDEN_LIFECYCLE_TABLE"}
        join_codes = {"UNAPPROVED_JOIN"}
        lifecycle = [issue.message for issue in report.issues if issue.code in lifecycle_codes]
        joins = [issue.message for issue in report.issues if issue.code in join_codes]
        business = [
            issue.message
            for issue in report.issues
            if issue.code not in lifecycle_codes | join_codes
        ]
        return EvaluationPolicyReport(
            valid=report.valid,
            violations=list(report.errors),
            lifecycle_violations=lifecycle,
            join_violations=joins,
            business_policy_violations=business,
        )
