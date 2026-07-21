class DataAssetAgentsError(Exception):
    """Base domain error safe to expose as a readable API message."""


class OntologyError(DataAssetAgentsError):
    """Raised when reviewed ontology data is invalid or incomplete."""


class OntologyConflictError(OntologyError):
    """Raised when an ontology Draft state transition is not allowed."""

    def __init__(
        self,
        message: str,
        code: str = "ONTOLOGY_CONFLICT",
        *,
        current_revision: int | None = None,
        current_hash: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision
        self.current_hash = current_hash


class OntologyGovernanceError(OntologyError):
    """Structured release-governance failure safe for API clients."""

    def __init__(
        self,
        message: str,
        code: str,
        *,
        current_revision: int | None = None,
        current_hash: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision
        self.current_hash = current_hash


class UnsafeSQLError(DataAssetAgentsError):
    """Raised when SQL violates the read-only execution policy."""


class QueryExecutionError(DataAssetAgentsError):
    """Raised when EXPLAIN or read-only query execution fails."""


class UnsupportedQueryError(DataAssetAgentsError):
    """Raised when the reviewed MVP ontology cannot answer a question."""

    def __init__(self, message: str, code: str = "UNSUPPORTED_QUERY") -> None:
        super().__init__(message)
        self.code = code
