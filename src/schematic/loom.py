"""Thin wrapper around the LOOM tool suite, run inside Docker.

Every LOOM tool reads a GeoJSON line graph on stdin and writes one on stdout,
so the whole suite composes as a pipe. ``run`` drives one tool; ``pipeline``
chains several. ``gtfs2graph`` (GTFS zip in) and ``transitmap`` (SVG out) are
the two ends that break the pattern and get their own helpers.

A caller that may need to stop a tool mid-run -- the JSON-RPC server, on a
``$/cancelRequest`` -- wraps its work in ``cancellable(job)``. Every process
started inside that block is registered on the ``Job``, ``Job.cancel()`` ends
it, and each line the tool writes to stderr reaches ``job.log`` as it arrives.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

IMAGE = "openschematicmaps/loom"
Graph = dict[str, Any]

# How many stderr lines an error message carries; LOOM is chatty on the way
# to a failure and the cause is at the end.
TAIL = 15


class LoomError(RuntimeError):
    pass


class Cancelled(LoomError):
    """The job was cancelled while a tool was running, or before it started."""


class Job:
    """A handle on the LOOM process running for one caller, so it can be cancelled."""

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self.log = log or (lambda line: None)
        self.cancelled = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> subprocess.Popen[bytes] | None:
        """The process the job is waiting on right now, if any."""
        with self._lock:
            return self._proc

    def cancel(self) -> None:
        """Mark the job cancelled and end the process it is running, if any.

        SIGTERM first: the Docker client forwards it to the tool inside the
        container, which exits and lets ``--rm`` clean up. SIGKILL only if the
        client does not go, since that one is not forwarded.
        """
        with self._lock:
            self.cancelled = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _attach(self, proc: subprocess.Popen[bytes]) -> bool:
        """Register the running process; True if the job was cancelled meanwhile."""
        with self._lock:
            self._proc = proc
            return self.cancelled

    def _detach(self) -> None:
        with self._lock:
            self._proc = None


_local = threading.local()


@contextmanager
def cancellable(job: Job) -> Iterator[Job]:
    """Run the block with ``job`` receiving every LOOM process it starts."""
    previous = getattr(_local, "job", None)
    _local.job = job
    try:
        yield job
    finally:
        _local.job = previous


def current_job() -> Job | None:
    return getattr(_local, "job", None)


def _docker(args: Sequence[str], *, mounts: Iterable[tuple[Path, str]] = ()) -> list[str]:
    if shutil.which("docker") is None:
        raise LoomError("docker not found on PATH")
    cmd = ["docker", "run", "--rm", "-i"]
    for host, container in mounts:
        cmd += ["-v", f"{host.resolve()}:{container}:ro"]
    cmd += [IMAGE, *args]
    return cmd


def _tool(cmd: Sequence[str]) -> str:
    return cmd[cmd.index(IMAGE) + 1] if IMAGE in cmd else cmd[0]


def _run(cmd: Sequence[str], stdin: bytes | None) -> bytes:
    job = current_job()
    if job is not None and job.cancelled:
        raise Cancelled(f"{_tool(cmd)}: cancelled before it started")

    # No payload means no stdin at all -- never the parent's. The server's
    # stdin is its protocol stream, and `docker run -i` would read from it.
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if job is not None and job._attach(proc):
        proc.kill()
    assert proc.stdout is not None and proc.stderr is not None

    tail: list[str] = []

    def pump_stderr() -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").rstrip("\r\n")
            tail.append(line)
            if len(tail) > TAIL:
                del tail[0]
            if job is not None:
                job.log(line)

    def feed_stdin() -> None:
        assert proc.stdin is not None and stdin is not None
        try:
            proc.stdin.write(stdin)
        except BrokenPipeError:
            pass  # the tool exited first; its stderr says why
        finally:
            proc.stdin.close()

    # stderr and stdin each get a thread so a tool that writes before it has
    # read everything, or fails before it has read anything, cannot deadlock
    # against this one reading stdout.
    threads = [threading.Thread(target=pump_stderr, daemon=True)]
    if stdin is not None:
        threads.append(threading.Thread(target=feed_stdin, daemon=True))
    for t in threads:
        t.start()
    out = proc.stdout.read()
    proc.wait()
    for t in threads:
        t.join()

    if job is not None:
        job._detach()
        if job.cancelled:
            raise Cancelled(f"{_tool(cmd)}: cancelled")
    if proc.returncode != 0:
        raise LoomError(f"{' '.join(cmd[-3:])} failed ({proc.returncode}):\n" + "\n".join(tail))
    return out


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
    proc = subprocess.run(_docker([tool, "-h"]), capture_output=True, stdin=subprocess.DEVNULL)
    return (proc.stdout + proc.stderr).decode(errors="replace")
