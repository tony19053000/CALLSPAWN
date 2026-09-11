"""Dependency graph over generated agents (CS-014).

Built from ``AgentSpec.dependencies``. Used by the factory to reject
unresolvable references and cycles, and by the runner to decide which agents
are ready, which must wait and which are blocked by an upstream failure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from callswarm.models import AgentSpec


class GraphError(ValueError):
    """The dependency graph is not a DAG with resolvable references."""


@dataclass(frozen=True)
class DependencyGraph:
    nodes: tuple[str, ...]
    edges: Mapping[str, frozenset[str]]  # node -> dependencies
    reverse: Mapping[str, frozenset[str]] = field(default_factory=dict)  # node -> dependents

    def dependencies_of(self, node: str) -> frozenset[str]:
        return self.edges.get(node, frozenset())

    def dependents_of(self, node: str) -> frozenset[str]:
        return self.reverse.get(node, frozenset())

    def roots(self) -> list[str]:
        return [n for n in self.nodes if not self.edges.get(n)]

    def leaves(self) -> list[str]:
        return [n for n in self.nodes if not self.reverse.get(n)]

    def ready(self, completed: Iterable[str], excluded: Iterable[str] = ()) -> list[str]:
        """Nodes whose every dependency is in ``completed`` and that are not excluded."""
        done = set(completed)
        skip = set(excluded)
        return [n for n in self.nodes if n not in skip and n not in done and self.edges[n] <= done]

    def downstream(self, node: str) -> set[str]:
        """Every transitive dependent of ``node``."""
        seen: set[str] = set()
        stack = [node]
        while stack:
            current = stack.pop()
            for dependent in self.reverse.get(current, ()):
                if dependent not in seen:
                    seen.add(dependent)
                    stack.append(dependent)
        return seen

    def topological_order(self) -> list[str]:
        """Kahn's algorithm; raises :class:`GraphError` on a cycle."""
        order, cyclic = _kahn(self.nodes, self.edges)
        if cyclic:
            raise GraphError(f"dependency cycle involving {sorted(cyclic)}")
        return order

    def cyclic_nodes(self) -> set[str]:
        """Nodes that are in a cycle or depend (transitively) on one."""
        _, cyclic = _kahn(self.nodes, self.edges)
        return cyclic


def _kahn(nodes: Iterable[str], edges: Mapping[str, frozenset[str]]) -> tuple[list[str], set[str]]:
    remaining = {n: set(edges.get(n, frozenset())) for n in nodes}
    order: list[str] = []
    while True:
        ready = sorted(n for n, deps in remaining.items() if not deps)
        if not ready:
            break
        for n in ready:
            order.append(n)
            del remaining[n]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order, set(remaining)


def unresolved_references(specs: Iterable[AgentSpec]) -> dict[str, list[str]]:
    """Spec id -> dependency ids that do not name a spec in the set."""
    specs = list(specs)
    ids = {s.id for s in specs}
    return {
        s.id: [d for d in s.dependencies if d not in ids]
        for s in specs
        if any(d not in ids for d in s.dependencies)
    }


def build_graph(specs: Iterable[AgentSpec], *, strict: bool = True) -> DependencyGraph:
    """Build the DAG. With ``strict`` (default) unresolved references and cycles raise."""
    specs = list(specs)
    ids = [s.id for s in specs]
    if len(set(ids)) != len(ids):
        raise GraphError("duplicate agent ids")
    if strict:
        unresolved = unresolved_references(specs)
        if unresolved:
            raise GraphError(f"unresolved dependencies: {unresolved}")
    edges = {s.id: frozenset(d for d in s.dependencies if d in ids and d != s.id) for s in specs}
    if strict and any(s.id in s.dependencies for s in specs):
        raise GraphError("an agent cannot depend on itself")
    reverse: dict[str, set[str]] = {i: set() for i in ids}
    for node, deps in edges.items():
        for dep in deps:
            reverse[dep].add(node)
    graph = DependencyGraph(
        nodes=tuple(ids),
        edges=edges,
        reverse={k: frozenset(v) for k, v in reverse.items()},
    )
    if strict:
        graph.topological_order()
    return graph
