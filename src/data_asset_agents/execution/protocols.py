from typing import Protocol

from data_asset_agents.text2sql.models import ExecutionResult


class ExecutorProtocol(Protocol):
    """Stable execution port used by the Text-to-SQL subgraph."""

    def execute(self, sql: str, allowed_tables: set[str]) -> ExecutionResult: ...
