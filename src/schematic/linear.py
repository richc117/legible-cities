"""Linear layout: every line as a row of evenly spaced stations.

The schematic map has already thrown away geography, but it is still a map. The
linear view throws away the last of it: each line becomes a horizontal row, its
stations equally spaced, so row length reads directly as station count and the
lines can be compared with each other rather than located in a city.

Most lines take to this happily -- 108 of the 172 across the registered
networks are a simple path from one terminus to the other. The rest branch, and
a single straight row would be a lie for them, so a branch forks onto a short
row of its own beginning at its junction, the way the diagram above a carriage
door does.

Only *columns* are assigned here. Vertical position depends on the sort order
the reader picks, so rows are placed in the browser.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .linegraph import LineGraph


@dataclass
class Row:
    """One horizontal run of stations: a line's spine, or one of its branches."""

    # 0 for the spine, 1+ for each branch, which sets how far it drops.
    depth: int
    # (node id, column) in running order.
    nodes: list[tuple[str, int]] = field(default_factory=list)

    @property
    def start_col(self) -> int:
        return min(c for _, c in self.nodes) if self.nodes else 0

    @property
    def end_col(self) -> int:
        return max(c for _, c in self.nodes) if self.nodes else 0


@dataclass
class Line:
    label: str
    rows: list[Row]

    @property
    def stations(self) -> int:
        return sum(len(r.nodes) for r in self.rows)

    @property
    def width(self) -> int:
        """Columns spanned, which is what sets the drawing width."""
        return max((r.end_col for r in self.rows), default=0) + 1


@dataclass
class LinearLayout:
    lines: list[Line]

    @property
    def columns(self) -> int:
        return max((l.width for l in self.lines), default=1)

    def column_of(self, label: str, node: str) -> int | None:
        for line in self.lines:
            if line.label != label:
                continue
            for row in line.rows:
                for nid, col in row.nodes:
                    if nid == node:
                        return col
        return None

    def to_json(self) -> dict:
        return {
            "columns": self.columns,
            "lines": [
                {
                    "label": l.label,
                    "stations": l.stations,
                    "rows": [{"depth": r.depth, "nodes": r.nodes} for r in l.rows],
                }
                for l in self.lines
            ],
        }


def _adjacency(graph: LineGraph, label: str) -> tuple[dict[str, set[str]], set[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for e in graph.edges_for(label):
        if e.src == e.dst:
            continue
        adj[e.src].add(e.dst)
        adj[e.dst].add(e.src)
        nodes |= {e.src, e.dst}
    return adj, nodes


def _farthest(adj: dict[str, set[str]], start: str) -> tuple[str, dict[str, str]]:
    """BFS: the most distant node from ``start``, and the tree of predecessors."""
    seen = {start}
    prev: dict[str, str] = {}
    last = start
    q = deque([start])
    while q:
        node = q.popleft()
        last = node
        for nxt in sorted(adj[node]):
            if nxt not in seen:
                seen.add(nxt)
                prev[nxt] = node
                q.append(nxt)
    return last, prev


def spine(adj: dict[str, set[str]], nodes: set[str]) -> list[str]:
    """The line's main run: the longest path between two of its stations.

    Double BFS, which is exact on a tree. A few lines contain a loop -- LA's A
    Line ends in one at Long Beach -- where it returns a reasonable long path
    rather than a provably maximal one. That is fine: the loop's stations still
    land somewhere sensible, and nothing is dropped.
    """
    if not nodes:
        return []
    start = min(nodes)
    a, _ = _farthest(adj, start)
    b, prev = _farthest(adj, a)

    path = [b]
    while path[-1] != a:
        nxt = prev.get(path[-1])
        if nxt is None:  # disconnected; take what we have
            break
        path.append(nxt)
    path.reverse()
    return path


def _branches(adj: dict[str, set[str]], nodes: set[str],
              on_spine: dict[str, int]) -> list[Row]:
    """Everything not on the spine, grouped into runs hanging off a junction."""
    rows: list[Row] = []
    placed: set[str] = set(on_spine)

    # Start from spine nodes so a branch's first station sits just past its
    # junction rather than at an arbitrary column.
    for junction in sorted(on_spine, key=lambda n: on_spine[n]):
        for neighbour in sorted(adj[junction]):
            if neighbour in placed:
                continue
            row = Row(depth=len(rows) + 1)
            col = on_spine[junction]
            queue = deque([neighbour])
            placed.add(neighbour)
            while queue:
                node = queue.popleft()
                col += 1
                row.nodes.append((node, col))
                for nxt in sorted(adj[node]):
                    if nxt not in placed:
                        placed.add(nxt)
                        queue.append(nxt)
            rows.append(row)

    # A component reachable from no spine node at all -- NYC labels three
    # unconnected shuttles "S". Give each its own row rather than losing the
    # stations. The remaining set is snapshotted, so skip anything an earlier
    # pass of this loop has already swallowed.
    for node in sorted(nodes - placed):
        if node in placed:
            continue
        row = Row(depth=len(rows) + 1)
        col = 0
        queue = deque([node])
        placed.add(node)
        while queue:
            current = queue.popleft()
            row.nodes.append((current, col))
            col += 1
            for nxt in sorted(adj[current]):
                if nxt not in placed:
                    placed.add(nxt)
                    queue.append(nxt)
        rows.append(row)
    return rows


def build(graph: LineGraph, order: list[str] | None = None) -> LinearLayout:
    """Lay out every line in the graph."""
    labels = order or graph.labels
    lines: list[Line] = []
    for label in labels:
        adj, nodes = _adjacency(graph, label)
        if not nodes:
            continue
        main = spine(adj, nodes)
        on_spine = {n: i for i, n in enumerate(main)}
        rows = [Row(depth=0, nodes=[(n, i) for i, n in enumerate(main)])]
        rows += _branches(adj, nodes, on_spine)
        lines.append(Line(label=label, rows=rows))
    return LinearLayout(lines=lines)
