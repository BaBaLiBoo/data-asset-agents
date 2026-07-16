from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from data_asset_agents.execution.protocols import ExecutorProtocol
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets.repository import HistoricalSQLRepository
from data_asset_agents.sql_assets.service import SQLAssetService
from data_asset_agents.text2sql.nodes import Text2SQLNodes
from data_asset_agents.text2sql.state import Text2SQLState

GRAPH_NODES = [
    "parse_semantic_query",
    "retrieve_business_concepts",
    "resolve_physical_assets",
    "plan_join_path",
    "retrieve_historical_sql",
    "generate_sql",
    "validate_sql",
    "research_validation_error",
    "repair_sql",
    "execute_sql",
    "explain_result",
]

GRAPH_EDGES = [
    ("START", "retrieve_business_concepts"),
    ("retrieve_business_concepts", "parse_semantic_query"),
    ("parse_semantic_query", "explain_result"),
    ("parse_semantic_query", "resolve_physical_assets"),
    ("resolve_physical_assets", "plan_join_path"),
    ("plan_join_path", "retrieve_historical_sql"),
    ("retrieve_historical_sql", "generate_sql"),
    ("generate_sql", "validate_sql"),
    ("generate_sql", "explain_result"),
    ("validate_sql", "execute_sql"),
    ("validate_sql", "research_validation_error"),
    ("validate_sql", "explain_result"),
    ("research_validation_error", "repair_sql"),
    ("research_validation_error", "explain_result"),
    ("repair_sql", "validate_sql"),
    ("repair_sql", "explain_result"),
    ("execute_sql", "explain_result"),
    ("execute_sql", "research_validation_error"),
    ("explain_result", "END"),
]


def build_text2sql_graph(
    ontology: OntologyService,
    executor: ExecutorProtocol,
    history: HistoricalSQLRepository | None = None,
    sql_assets: SQLAssetService | None = None,
    history_enabled: bool = True,
) -> CompiledStateGraph:
    """Build a standalone compiled graph suitable for embedding as a subgraph."""

    nodes = Text2SQLNodes(
        ontology,
        executor,
        history,
        sql_assets,
        history_enabled=history_enabled,
    )
    graph = StateGraph(Text2SQLState)
    for name in GRAPH_NODES:
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "retrieve_business_concepts")
    graph.add_edge("retrieve_business_concepts", "parse_semantic_query")

    def route_parse(
        state: Text2SQLState,
    ) -> Literal["resolve_physical_assets", "explain_result"]:
        return (
            "resolve_physical_assets"
            if state.get("status") == "success"
            else "explain_result"
        )

    graph.add_conditional_edges("parse_semantic_query", route_parse)
    graph.add_edge("resolve_physical_assets", "plan_join_path")
    graph.add_edge("plan_join_path", "retrieve_historical_sql")
    graph.add_edge("retrieve_historical_sql", "generate_sql")

    def route_generation(
        state: Text2SQLState,
    ) -> Literal["validate_sql", "explain_result"]:
        return "validate_sql" if state.get("generated_sql") else "explain_result"

    graph.add_conditional_edges("generate_sql", route_generation)

    def route_validation(
        state: Text2SQLState,
    ) -> Literal["execute_sql", "research_validation_error", "explain_result"]:
        if state["validation_report"].valid:
            return "execute_sql"
        if state.get("retry_count", 0) < 2:
            return "research_validation_error"
        return "explain_result"

    graph.add_conditional_edges("validate_sql", route_validation)

    def route_research(
        state: Text2SQLState,
    ) -> Literal["repair_sql", "explain_result"]:
        if state.get("repairable") and state.get("retry_count", 0) < 2:
            return "repair_sql"
        return "explain_result"

    graph.add_conditional_edges("research_validation_error", route_research)

    def route_repair(
        state: Text2SQLState,
    ) -> Literal["validate_sql", "explain_result"]:
        return "validate_sql" if state.get("sql_changed") else "explain_result"

    graph.add_conditional_edges("repair_sql", route_repair)

    def route_execution(
        state: Text2SQLState,
    ) -> Literal["explain_result", "research_validation_error"]:
        if state["validation_report"].explain_passed:
            return "explain_result"
        return "research_validation_error"

    graph.add_conditional_edges("execute_sql", route_execution)
    graph.add_edge("explain_result", END)
    return graph.compile()
