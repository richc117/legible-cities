"""A stage graph in fields a sentence can be written from (engine issue 54).

The desktop app gives its drawing of a stage a text alternative, and may not
work one out for itself: the engine is the one account of the network, so it
says where each line runs and the app writes the words. Nothing here writes a
sentence, reads a file or reads the clock.

Per line, in ``LineGraph.labels`` order: its spine (``linear.spine``) gives
the two termini and the stations in order; everything off the spine is a
branch, a simple path in travel order from the station it leaves; a line
whose graph is one cycle is a loop, with one terminus; ``meets`` names the
other lines at each of its stations. A station is named as the map draws it,
the empty string where the feed gives no name and never its id; a junction
LOOM inserted is never named, listed or met at.

The minutes (``trip``, ``extent``) are of the layout and its service day, not
of the stage: ``pipeline.line_minutes`` reads them once per layout and day,
and this module only puts each trip's two names in the stage's order.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Mapping

from .linear import adjacency_for, spine
from .linegraph import LineGraph, Node
from .names import display_name


def station_name(node: Node) -> str:
    """The name the map draws for a station; the empty string for none."""
    return display_name(node.station_label or "")


def describe(graph: LineGraph, minutes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The protocol's StageDescription of ``graph``.

    ``minutes`` is ``pipeline.line_minutes``' answer for the layout and day,
    ``{label: {"minutes", "from", "to"} | None}``; without it every ``trip``
    and the ``extent`` are null.
    """
    carried: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        for ln in e.lines:
            carried[e.src].add(ln.label)
            carried[e.dst].add(ln.label)

    lines = [_line(graph, label, carried, minutes) for label in graph.labels]
    timed = [line for line in lines if line["trip"] is not None]
    extent = None
    if timed:
        # max keeps the first of equals, which is the first in label order.
        longest = max(timed, key=lambda line: line["trip"]["minutes"])
        extent = {"minutes": longest["trip"]["minutes"], "line": longest["label"],
                  "from": longest["trip"]["from"], "to": longest["trip"]["to"]}
    return {"extent": extent, "lines": lines}


def _line(graph: LineGraph, label: str, carried: dict[str, set[str]],
          minutes: Mapping[str, Any] | None) -> dict[str, Any]:
    adj, nodes = adjacency_for(graph, label)

    def is_station(n: str) -> bool:
        node = graph.nodes.get(n)
        return node is not None and node.is_station

    def name(n: str) -> str:
        return station_name(graph.nodes[n])

    loop = _is_loop(adj, nodes)
    if loop:
        run, branches = _cycle(adj, nodes, is_station), []
    else:
        run, branches = _spine_and_branches(adj, nodes, is_station, name)

    stations = [n for n in run if is_station(n)]
    termini = stations[:1] if loop else _ends(stations)
    # The line's every station, each once: the spine's, then each branch's.
    every = stations + [n for _at, ids in branches for n in ids]
    meets = []
    for n in every:
        others = carried[n] - {label}
        if others:
            # Sorted is labels order: LineGraph.labels sorts.
            meets.append({"station": name(n), "lines": sorted(others)})
    order = [name(n) for n in every]
    found = minutes.get(label) if minutes is not None else None
    return {"label": label,
            "termini": [name(n) for n in termini],
            "stations": [name(n) for n in stations],
            "meets": meets,
            "branches": [{"at": at, "stations": [name(n) for n in ids]} for at, ids in branches],
            "trip": _in_order(found, order)}


def _ends(stations: list[str]) -> list[str]:
    """The first and last station of a run; one when it holds one."""
    if len(stations) <= 1:
        return list(stations)
    return [stations[0], stations[-1]]


def _in_order(trip: Mapping[str, Any] | None, order: list[str]) -> dict[str, Any] | None:
    """``trip``'s two names in the order the line lists them; a name it does
    not list keeps the trip's own order."""
    if trip is None:
        return None
    a, b = trip["from"], trip["to"]
    if a in order and b in order and order.index(b) < order.index(a):
        a, b = b, a
    return {"minutes": trip["minutes"], "from": a, "to": b}


def _connected(adj: dict[str, set[str]], start: str) -> set[str]:
    seen = {start}
    queue = deque([start])
    while queue:
        for nxt in adj[queue.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _is_loop(adj: dict[str, set[str]], nodes: set[str]) -> bool:
    """One connected cycle: three nodes or more, every one of degree two.
    Asked first, because ``linear.spine`` on a cycle answers a long path."""
    return (len(nodes) >= 3 and all(len(adj[n]) == 2 for n in nodes)
            and len(_connected(adj, min(nodes))) == len(nodes))


def _cycle(adj: dict[str, set[str]], nodes: set[str], is_station) -> list[str]:
    """The cycle walked once: from the station with the smallest node id,
    first toward that station's neighbour with the smaller node id."""
    stations = [n for n in nodes if is_station(n)]
    start = min(stations) if stations else min(nodes)
    walk = [start]
    prev, here = start, min(adj[start])
    while here != start:
        walk.append(here)
        prev, here = here, next(n for n in adj[here] if n != prev)
    return walk


def _spine_and_branches(adj: dict[str, set[str]], nodes: set[str], is_station,
                        name) -> tuple[list[str], list[tuple[str | None, list[str]]]]:
    """The spine, in the piece holding the smallest node id, and the
    branches as ``(at, station node ids)``: those hanging from the spine,
    then each further piece in order of its smallest node id, as a branch
    with ``at`` None holding its own spine, followed by its own."""
    main = spine(adj, nodes)
    placed = set(main)
    branches: list[tuple[str | None, list[str]]] = []
    _hang(main, adj, placed, branches, is_station, name)
    for start in sorted(nodes):
        if start in placed:
            continue
        # The first unplaced node met in sorted order is its piece's
        # smallest: no branch reaches into a piece that touches nothing.
        piece = spine(adj, _connected(adj, start))
        placed.update(piece)
        _record(branches, None, piece, is_station)
        _hang(piece, adj, placed, branches, is_station, name)
    return main, branches


def _hang(run: list[str], adj: dict[str, set[str]], placed: set[str],
          branches: list[tuple[str | None, list[str]]], is_station, name) -> None:
    """Every branch reached from ``run``, in run order and then in node id
    order, each followed by the branches that fork from it, depth first.

    A branch is a simple path in travel order away from the node it leaves,
    extended while exactly one unplaced neighbour remains. Where it forks
    again it ends at the fork, and each way on is a branch of its own.
    ``linear._branches`` lists a forking branch breadth-first, which suits
    the page's rows and not a branch read in travel order, so it is not
    reused here.
    """
    for i, node in enumerate(run):
        for first in sorted(adj[node]):
            if first in placed:
                continue
            at = name(node) if is_station(node) else _beside(run, i, is_station, name)
            # A stack rather than recursion: a stage graph may fork deeper
            # than Python's recursion allows. Each is (first node, at).
            todo: list[tuple[str, str | None]] = [(first, at)]
            while todo:
                start, start_at = todo.pop()
                if start in placed:  # an earlier way on came round to it
                    continue
                path = [start]
                placed.add(start)
                while True:
                    ahead = sorted(n for n in adj[path[-1]] if n not in placed)
                    if len(ahead) != 1:
                        break
                    placed.add(ahead[0])
                    path.append(ahead[0])
                _record(branches, start_at, path, is_station)
                if len(ahead) >= 2:
                    fork = path[-1]
                    if is_station(fork):
                        fork_at = name(fork)
                    else:
                        # A fork ending a branch has one side: back along it.
                        back = [n for n in path[:-1] if is_station(n)]
                        fork_at = name(back[-1]) if back else start_at
                    todo.extend((n, fork_at) for n in reversed(ahead))


def _beside(run: list[str], i: int, is_station, name) -> str | None:
    """The station next to the junction ``run[i]``, on the side with more
    stations to the run's end; toward the run's start on a tie. None when
    the run has no station at all."""
    before = [n for n in run[:i] if is_station(n)]
    after = [n for n in run[i + 1:] if is_station(n)]
    if len(after) > len(before):
        return name(after[0])
    return name(before[-1]) if before else None


def _record(branches: list[tuple[str | None, list[str]]], at: str | None, path: list[str],
            is_station) -> None:
    """A branch, by its stations; one that passes no station names nothing
    and is left out, though the branches beyond it are not."""
    stations = [n for n in path if is_station(n)]
    if stations:
        branches.append((at, stations))
