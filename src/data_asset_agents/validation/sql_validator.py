from collections.abc import Iterable

from data_asset_agents.ontology.models import OntologyBundle
from data_asset_agents.text2sql.models import QueryFilter, ValidationReport
from data_asset_agents.validation.common import (
    CommonSQLSafetyValidator,
    DatabaseCatalog,
)
from data_asset_agents.validation.ontology_policy import OntologyPolicyValidator


class SQLValidator:
    """Compatibility facade combining public safety and optional ontology policy."""

    def __init__(self, ontology: OntologyBundle | None = None) -> None:
        self.ontology = ontology
        catalog = (
            DatabaseCatalog.from_table_columns(
                {asset.name: set(asset.columns) for asset in ontology.tables}
            )
            if ontology
            else DatabaseCatalog()
        )
        self.common = CommonSQLSafetyValidator(catalog)
        self.policy = OntologyPolicyValidator(ontology) if ontology else None

    def validate(
        self,
        sql: str,
        allowed_tables: set[str] | None = None,
        required_filters: Iterable[QueryFilter] = (),
    ) -> ValidationReport:
        common = self.common.validate(sql, allowed_tables)
        if not self.policy:
            return common
        policy = self.policy.validate(sql, required_filters=required_filters)
        issues = list(common.issues)
        for issue in policy.issues:
            if issue not in issues:
                issues.append(issue)
        return common.model_copy(
            update={
                "valid": not issues,
                "errors": [issue.message for issue in issues],
                "issues": issues,
            }
        )
