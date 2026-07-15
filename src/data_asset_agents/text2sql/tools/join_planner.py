from itertools import pairwise

import networkx as nx

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import JoinDefinition
from data_asset_agents.text2sql.models import JoinPlan, JoinStep


class JoinPlanner:
    """Plan the smallest reviewed join path between selected physical tables."""

    def __init__(self, joins: list[JoinDefinition]) -> None:
        self.graph = nx.Graph()
        for join in joins:
            if join.enabled:
                self.graph.add_edge(join.left_table, join.right_table, definition=join)

    def plan(self, required_tables: list[str]) -> JoinPlan:
        if not required_tables:
            return JoinPlan()
        path_nodes: list[str] = [required_tables[0]]
        steps: list[JoinStep] = []
        connected = {required_tables[0]}
        for target in required_tables[1:]:
            best_path: list[str] | None = None
            for source in connected:
                try:
                    candidate = nx.shortest_path(self.graph, source, target)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                if best_path is None or len(candidate) < len(best_path):
                    best_path = candidate
            if best_path is None:
                raise OntologyError(f"No reviewed join path connects table {target}")
            for left, right in pairwise(best_path):
                definition: JoinDefinition = self.graph[left][right]["definition"]
                step = JoinStep(
                    left_table=definition.left_table,
                    right_table=definition.right_table,
                    condition=definition.expression,
                    relationship=definition.relationship,
                )
                if step not in steps:
                    steps.append(step)
            path_nodes.extend(node for node in best_path if node not in path_nodes)
            connected.update(best_path)
        return JoinPlan(tables=path_nodes, steps=steps)

