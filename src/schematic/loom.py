"""Thin wrapper around the LOOM tool suite, run inside Docker.

Every LOOM tool reads a GeoJSON line graph on stdin and writes one on stdout,
so the whole suite composes as a pipe. ``run`` drives one tool; ``pipeline``
chains several. ``gtfs2graph`` (GTFS zip in) and ``transitmap`` (SVG out) are
the two ends that break the pattern and get their own helpers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence

IMAGE = "openschematicmaps/loom"
Graph = dict[str, Any]


class LoomError(RuntimeError):
    pass


def _docker(args: Sequence[str], *, mounts: Iterable[tuple[Path, str]] = ()) -> list[str]:
    if shutil.which("docker") is None:
        raise LoomError("docker not found on PATH")
    cmd = ["docker", "run", "--rm", "-i"]
    for host, container in mounts:
        cmd += ["-v", f"{host.resolve()}:{container}:ro"]
    cmd += [IMAGE, *args]
    return cmd


def _run(cmd: Sequence[str], stdin: bytes | None) -> bytes:
    proc = subprocess.run(cmd, input=stdin, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-15:]
        raise LoomError(f"{' '.join(cmd[-3:])} failed ({proc.returncode}):\n" + "\n".join(tail))
    return proc.stdout


def run(tool: str, graph: Graph | bytes, *args: str) -> Graph:
    """Run one graph-to-graph tool (topo, loom, octi)."""
    payload = graph if isinstance(graph, bytes) else json.dumps(graph).encode()
    return json.loads(_run(_docker([tool, *args]), payload))


def pipeline(graph: Graph | bytes, *stages: str | tuple[str, ...]) -> Graph:
    """Chain graph-to-graph tools: ``pipeline(g, "topo", "loom", ("octi", "-b", "orthoradial"))``."""
    payload = graph if isinstance(graph, bytes) else json.dumps(graph).encode()
    for stage in stages:
        tool, *args = (stage,) if isinstance(stage, str) else stage
        payload = _run(_docker([tool, *args]), payload)
    return json.loads(payload)


def gtfs2graph(feed_zip: Path, *args: str) -> Graph:
    """Convert a GTFS zip into a LOOM line graph."""
    mount = (feed_zip.parent, "/feed")
    cmd = _docker(["gtfs2graph", *args, f"/feed/{feed_zip.name}"], mounts=[mount])
    return json.loads(_run(cmd, None))


def transitmap(graph: Graph | bytes, *args: str) -> str:
    """Render a line graph to SVG."""
    payload = graph if isinstance(graph, bytes) else json.dumps(graph).encode()
    return _run(_docker(["transitmap", *args]), payload).decode()


def help_text(tool: str) -> str:
    """Capture a tool's -h output; the READMEs omit most options."""
    proc = subprocess.run(_docker([tool, "-h"]), capture_output=True)
    return (proc.stdout + proc.stderr).decode(errors="replace")
