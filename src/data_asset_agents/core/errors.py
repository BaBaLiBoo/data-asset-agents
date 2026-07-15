class DataAssetAgentsError(Exception):
    """Base domain error safe to expose as a readable API message."""


class OntologyError(DataAssetAgentsError):
    """Raised when reviewed ontology data is invalid or incomplete."""


class UnsafeSQLError(DataAssetAgentsError):
    """Raised when SQL violates the read-only execution policy."""


class QueryExecutionError(DataAssetAgentsError):
    """Raised when EXPLAIN or read-only query execution fails."""

