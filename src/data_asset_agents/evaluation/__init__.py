"""Text-to-SQL benchmark and regression evaluation package."""

from data_asset_agents.evaluation.models import (
    BenchmarkCase,
    BenchmarkSuite,
    EvaluationCaseResult,
    EvaluationComparison,
    EvaluationMetrics,
    EvaluationRun,
    EvaluationRunRequest,
    StrategyResult,
    TokenUsage,
)
from data_asset_agents.evaluation.normalizer import ResultNormalizer
from data_asset_agents.evaluation.physical_rag import (
    PhysicalRAGDocument,
    PhysicalRAGIndex,
    PhysicalRAGSearchResult,
)
from data_asset_agents.evaluation.strategies import (
    OntologyStrategy,
    PhysicalRAGStrategy,
    QueryStrategy,
    SchemaBaselineStrategy,
    StrategyRouter,
)

__all__ = [
    "BenchmarkCase",
    "BenchmarkSuite",
    "EvaluationCaseResult",
    "EvaluationComparison",
    "EvaluationMetrics",
    "EvaluationRun",
    "EvaluationRunRequest",
    "OntologyStrategy",
    "PhysicalRAGDocument",
    "PhysicalRAGIndex",
    "PhysicalRAGSearchResult",
    "PhysicalRAGStrategy",
    "QueryStrategy",
    "ResultNormalizer",
    "SchemaBaselineStrategy",
    "StrategyResult",
    "StrategyRouter",
    "TokenUsage",
]
