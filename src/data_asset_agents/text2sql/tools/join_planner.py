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

    @staticmethod
    def _orient_step(
        definition: JoinDefinition,
        joined_table: str,
        new_table: str,
    ) -> JoinStep:
        """Orient an undirected graph edge from an already joined table to a new table."""

        if (
            definition.left_table == joined_table
            and definition.right_table == new_table
        ):
            left_column = definition.left_column
            right_column = definition.right_column
            relationship = definition.relationship
        elif (
            definition.right_table == joined_table
            and definition.left_table == new_table
        ):
            left_column = definition.right_column
            right_column = definition.left_column
            relationship = {
                "many_to_one": "one_to_many",
                "one_to_many": "many_to_one",
            }.get(definition.relationship, definition.relationship)
        else:
            raise OntologyError(
                f"Join definition does not connect {joined_table} and {new_table}"
            )
        return JoinStep(
            left_table=joined_table,
            right_table=new_table,
            left_column=left_column,
            right_column=right_column,
            condition=(
                f"{joined_table}.{left_column} = {new_table}.{right_column}"
            ),
            relationship=relationship,
        )

    def plan(self, required_tables: list[str]) -> JoinPlan:
        if not required_tables:
            return JoinPlan()
        path_nodes: list[str] = [required_tables[0]]
        steps: list[JoinStep] = []
        connected = {required_tables[0]}
        for target in required_tables[1:]:
            if target in connected:
                continue
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
                if left not in connected and right in connected:
                    left, right = right, left
                if left not in connected:
                    raise OntologyError(
                        f"Join path is not connected before adding table {right}"
                    )
                if right in connected:
                    continue
                definition: JoinDefinition = self.graph[left][right]["definition"]
                step = self._orient_step(definition, left, right)
                if step not in steps:
                    steps.append(step)
                connected.add(right)
            path_nodes.extend(node for node in best_path if node not in path_nodes)
        return JoinPlan(tables=path_nodes, steps=steps)
