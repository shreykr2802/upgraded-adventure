"""
passes/toposort.py
──────────────────
Generic topological sort for dependency ordering within a migration pass.

Each pass converts items that may depend on each other:
  - a model references other models (UserModel → AddressModel)
  - a component composes other components (organism → molecule → atom)
  - a page uses hooks + components + layout

This sorts items so dependencies are converted BEFORE the things that use them.
C# allows circular references (A → B → A); TypeScript interfaces tolerate
cycles, so we DETECT and report cycles but do not block — cyclic nodes are
emitted in a stable order and flagged for the caller.

Pure function, no project dependencies — fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SortResult:
    ordered: list[str]                       # items in dependency-first order
    cycles: list[list[str]] = field(default_factory=list)   # detected cycles
    missing: list[str] = field(default_factory=list)        # deps not in node set

    @property
    def has_cycles(self) -> bool:
        return len(self.cycles) > 0


def toposort(graph: dict[str, list[str]]) -> SortResult:
    """
    Order nodes so that each node comes after all of its dependencies.

    Args:
        graph: {node: [deps]} — `node` depends on each item in `deps`.
               Deps not present as keys are treated as external (recorded in
               `missing`) and don't block ordering.

    Returns:
        SortResult with the dependency-first order, any cycles, and missing deps.
    """
    nodes = set(graph.keys())

    # Record deps that aren't nodes themselves (external/unknown)
    missing: set[str] = set()
    for deps in graph.values():
        for d in deps:
            if d not in nodes:
                missing.add(d)

    # Kahn's algorithm with deterministic ordering (sorted) for stable output.
    # in_degree counts only dependencies that are actual nodes.
    in_degree: dict[str, int] = {n: 0 for n in nodes}
    dependents: dict[str, list[str]] = {n: [] for n in nodes}

    for node, deps in graph.items():
        for d in deps:
            if d in nodes:
                in_degree[node] += 1
                dependents[d].append(node)

    # Start with nodes that have no (internal) dependencies.
    ready = sorted([n for n in nodes if in_degree[n] == 0])
    ordered: list[str] = []

    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for dep in sorted(dependents[node]):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                ready.append(dep)
        ready.sort()

    # Anything not ordered is part of a cycle.
    cycles: list[list[str]] = []
    if len(ordered) < len(nodes):
        remaining = nodes - set(ordered)
        cycles = _find_cycles({n: [d for d in graph[n] if d in remaining]
                               for n in remaining})
        # Append cyclic nodes in a stable order so the pass can still proceed.
        ordered.extend(sorted(remaining))

    return SortResult(ordered=ordered, cycles=cycles, missing=sorted(missing))


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Find simple cycles via DFS (for reporting only)."""
    cycles: list[list[str]] = []
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    stack: list[str] = []

    def dfs(node: str):
        color[node] = GREY
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GREY:
                # found a back-edge → cycle from dep to node
                if dep in stack:
                    idx = stack.index(dep)
                    cycles.append(stack[idx:] + [dep])
            elif color[dep] == WHITE:
                dfs(dep)
        stack.pop()
        color[node] = BLACK

    for n in sorted(graph.keys()):
        if color[n] == WHITE:
            dfs(n)
    return cycles
