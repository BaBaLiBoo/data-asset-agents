from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from data_asset_agents.execution.executor import ExecutorProtocol
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets.repository import HistoricalSQLRepository
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
    ("START", "parse_semantic_query"),
    ("parse_semantic_query", "retrieve_business_concepts"),
    ("retrieve_business_concepts", "resolve_physical_assets"),
    ("resolve_physical_assets", "plan_join_path"),
    ("plan_join_path", "retrieve_historical_sql"),
    ("retrieve_historical_sql", "generate_sql"),
    ("generate_sql", "validate_sql"),
    ("validate_sql", "execute_sql"),
    ("validate_sql", "research_validation_error"),
    ("research_validation_error", "repair_sql"),
    ("repair_sql", "validate_sql"),
    ("execute_sql", "explain_result"),
    ("execute_sql", "research_validation_error"),
    ("validate_sql", "explain_result"),
    ("explain_result", "END"),
]


def build_text2sql_graph(
    ontology: OntologyService,
    executor: ExecutorProtocol,
    history: HistoricalSQLRepository | None = None,
) -> CompiledStateGraph:
    """Build a standalone compiled graph suitable for embedding as a LangGraph subgraph."""

    nodes = Text2SQLNodes(ontology, executor, history)
    graph = StateGraph(Text2SQLState)
    for name in GRAPH_NODES:
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "parse_semantic_query")
    for source, target in GRAPH_EDGES[1:7]:
        graph.add_edge(source, target)

    def route_validation(state: Text2SQLState) -> Literal["execute_sql", "research_validation_error", "explain_result"]:
        if state["validation_report"].valid:
            return "execute_sql"
        if state.get("retry_count", 0) < 2:
            return "research_validation_error"
        return "explain_result"

    graph.add_conditional_edges("validate_sql", route_validation)
    graph.add_edge("research_validation_error", "repair_sql")
    graph.add_edge("repair_sql", "validate_sql")

    def route_execution(
        state: Text2SQLState,
    ) -> Literal["explain_result", "research_validation_error"]:
        if state["validation_report"].explain_passed:
            return "explain_result"
        if state.get("retry_count", 0) < 2:
            return "research_validation_error"
        return "explain_result"

    graph.add_conditional_edges("execute_sql", route_execution)
    graph.add_edge("explain_result", END)
    return graph.compile()
