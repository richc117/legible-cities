"""Thin wrapper around the LOOM tool suite, behind one of two backends.

Every LOOM tool reads a GeoJSON line graph on stdin and writes one on stdout,
so the whole suite composes as a pipe. ``run`` drives one tool; ``pipeline``
chains several. ``gtfs2graph`` (a feed in) and ``transitmap`` (SVG out) are
the two ends that break the pattern and get their own helpers.

Two backends. ``DockerBackend`` runs each tool in the image ``docker/``
builds, which is how this repository has always worked. ``NativeBackend``
runs binaries from a directory, which is what the desktop app ships: built
in its own CI from a pinned commit, without the optional solvers and without
libzip, so its ``gtfs2graph`` is handed the feed unpacked rather than the
zip. ``backend()`` picks by ``SCHEMATIC_LOOM_BIN``: set, the directory it
names; unset, Docker. The commit the binaries came from has to be told
rather than asked -- they answer ``--version`` with ``-128-NOTFOUND``, there
being no git tree where they were built -- so the host passes it in
``SCHEMATIC_LOOM_COMMIT`` and ``engine.info`` reports it, or ``null``.

Every process gets a timeout (``TIMEOUT`` unless the call says otherwise),
which ends it and raises with the tail of its stderr; streams that stderr to
the current job's log; can be ended through the job on a cancel; and is
given no stdin unless it has a payload. A caller that may need to stop a
tool mid-run -- the JSON-RPC server, on a ``$/cancelRequest`` -- wraps its
work in ``cancellable(job)``. Every process started inside that block is
registered on the ``Job`` and ``Job.cancel()`` ends it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

IMAGE = "openschematicmaps/loom"
Graph = dict[str, Any]

BIN_ENV = "SCHEMATIC_LOOM_BIN"
COMMIT_ENV = "SCHEMATIC_LOOM_COMMIT"

# The four tools a native directory holds, in pipeline order. The Docker
# image has more (``transitmap``); the app ships these.
TOOLS = ("gtfs2graph", "topo", "loom", "octi")

# Seconds one tool may run before it is ended. LOOM can wedge on bad input,
# and a process nobody bounds is a process nobody can explain; the largest
# registered network takes minutes, not half an hour. A call that knows
# better passes its own, or ``None`` for no limit.
TIMEOUT: float | None = 1800.0

# How many stderr lines an error message carries; LOOM is chatty on the way
# to a failure and the cause is at the end.
TAIL = 15

# On Windows a child without this flag opens a console window for every
# stage. Elsewhere the flag does not exist and the value is 0.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LoomError(RuntimeError):
    pass


class Cancelled(LoomError):
    """The job was cancelled while a tool was running, or before it started."""


# ---------------------------------------------------------------- backends


class Backend:
    """How a tool is started. ``command`` is the argument list for a graph
    tool; ``feed_command`` the one for ``gtfs2graph``, whose input is a feed
    on disk rather than a graph on stdin."""

    name: str

    def command(self, tool: str, args: Sequence[str] = ()) -> list[str]:
        raise NotImplementedError

    def feed_command(self, feed_zip: Path, args: Sequence[str] = ()) -> list[str]:
        raise NotImplementedError


class DockerBackend(Backend):
    """Each tool in a fresh container from the image ``docker/`` builds."""

    name = "docker"

    def __init__(self, image: str = IMAGE) -> None:
        self.image = image

    def command(self, tool: str, args: Sequence[str] = (), *,
                mounts: Iterable[tuple[Path, str]] = ()) -> list[str]:
        if shutil.which("docker") is None:
            raise LoomError("docker not found on PATH")
        # --init puts tini at PID 1 and the tool under it. Without it the
        # tool is PID 1 of its namespace, and a PID 1 with no handler ignores
        # the SIGTERM the client forwards on a cancel or a timeout: the client
        # dies, the container runs on until the tool finishes by itself, and
        # --rm never fires. With it the signal reaches the tool and it dies by
        # default action, which is what "cancel" and "timeout" have to mean.
        cmd = ["docker", "run", "--rm", "-i", "--init"]
        for host, container in mounts:
            cmd += ["-v", f"{host.resolve()}:{container}:ro"]
        cmd += [self.image, tool, *args]
        return cmd

    def feed_command(self, feed_zip: Path, args: Sequence[str] = ()) -> list[str]:
        mount = (feed_zip.parent, "/feed")
        return self.command("gtfs2graph", [*args, f"/feed/{feed_zip.name}"], mounts=[mount])


class NativeBackend(Backend):
    """The tools as binaries in one directory: ``gtfs2graph``, ``topo``,
    ``loom`` and ``octi``, with ``.exe`` on Windows, and their DLLs beside
    them there."""

    name = "native"

    def __init__(self, bin_dir: Path) -> None:
        self.bin_dir = bin_dir

    def executable(self, tool: str) -> Path:
        for candidate in (self.bin_dir / tool, self.bin_dir / f"{tool}.exe"):
            if candidate.is_file():
                return candidate
        raise LoomError(f"{BIN_ENV} names {self.bin_dir}, which has no {tool}")

    def command(self, tool: str, args: Sequence[str] = ()) -> list[str]:
        return [str(self.executable(tool)), *args]

    def feed_command(self, feed_zip: Path, args: Sequence[str] = ()) -> list[str]:
        # A build without libzip exits 1 on a zip; a directory of the
        # feed's tables is what it reads.
        return self.command("gtfs2graph", [*args, str(unpacked(feed_zip))])


def backend() -> Backend:
    """The backend the environment asks for: native from ``SCHEMATIC_LOOM_BIN``
    when it is set, else Docker. Read on every call, like the home, so a host
    that sets the variable after import is seen."""
    value = os.environ.get(BIN_ENV, "")
    if not value:
        return DockerBackend()
    return NativeBackend(Path(os.path.expanduser(value)).absolute())


def commit() -> str | None:
    """The LOOM commit the host says its binaries were built from, or None.

    Told, not asked: the binaries carry no version, and the Docker image's is
    whatever ``LOOM_REF`` the build was given."""
    value = os.environ.get(COMMIT_ENV, "").strip()
    return value or None


_unpacking = threading.Lock()


def unpacked(feed_zip: Path) -> Path:
    """The feed's tables as a directory beside the zip, unpacked once and
    again whenever the zip is newer.

    Written whole into a scratch directory and moved into place, so an
    engine killed mid-unpack leaves nothing that looks finished; the scratch
    name is not ``feeds.normalize``'s ``.tmp``, which is a file. One lock,
    because two projects on one feed can lay out at once and the second must
    not remove what the first is extracting into or reading from. A cancel
    is honoured once the unpack has finished, which is seconds: it ends
    processes, and this is not one."""
    directory = feed_zip.with_suffix("")
    with _unpacking:
        if directory.is_dir() and directory.stat().st_mtime >= feed_zip.stat().st_mtime:
            return directory
        scratch = directory.with_name(directory.name + ".unpacking")
        shutil.rmtree(scratch, ignore_errors=True)
        with zipfile.ZipFile(feed_zip) as zf:
            zf.extractall(scratch)
        shutil.rmtree(directory, ignore_errors=True)
        os.replace(scratch, directory)
        return directory


# -------------------------------------------------------------------- jobs


def _end(proc: subprocess.Popen[bytes]) -> None:
    """End a process: SIGTERM first, SIGKILL if it stays.

    The Docker client forwards a SIGTERM to the tool inside the container,
    which exits and lets ``--rm`` clean up; it does not forward a SIGKILL, so
    that comes second and only if the client will not go. A native tool
    answers either directly."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


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

        Returns once the process has gone: at once when SIGTERM is honoured,
        which both backends do, and after the five-second grace otherwise.
        The server calls this from its reader thread."""
        with self._lock:
            self.cancelled = True
            proc = self._proc
        if proc is not None:
            _end(proc)

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


# ----------------------------------------------------------------- running


def execute(tool: str, stdin: bytes | None, *, args: Sequence[str] = (),
            timeout: float | None = TIMEOUT, feed: Path | None = None,
            on_stderr: Callable[[str], None] | None = None) -> bytes:
    """Run one tool on the current backend and return its stdout.

    ``stdin`` is the tool's payload; ``None`` means no stdin at all, never the
    parent's -- the server's stdin is its protocol stream, and ``docker run
    -i`` would read from it. ``feed`` makes this a ``gtfs2graph`` call, whose
    input is the feed on disk. Each stderr line goes to ``on_stderr`` and to
    the current job's log as it arrives. A tool that outlives ``timeout`` is
    ended and raises ``LoomError`` with the tail; one ended by a cancel
    raises ``Cancelled``.
    """
    job = current_job()
    if job is not None and job.cancelled:
        raise Cancelled(f"{tool}: cancelled before it started")

    chosen = backend()
    cmd = chosen.feed_command(feed, args) if feed is not None else chosen.command(tool, args)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW)
    if job is not None and job._attach(proc):
        _end(proc)
    assert proc.stdout is not None and proc.stderr is not None

    tail: list[str] = []

    def pump_stderr() -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").rstrip("\r\n")
            tail.append(line)
            if len(tail) > TAIL:
                del tail[0]
            if on_stderr is not None:
                on_stderr(line)
            if job is not None:
                job.log(line)

    def feed_stdin() -> None:
        assert proc.stdin is not None and stdin is not None
        # A tool that exits before it has read its payload breaks the pipe
        # on the write and again on the close, which flushes what the write
        # left buffered; both are the tool's business and its stderr says why.
        try:
            proc.stdin.write(stdin)
        except OSError:
            pass
        try:
            proc.stdin.close()
        except OSError:
            pass

    # stderr and stdin each get a thread so a tool that writes before it has
    # read everything, or fails before it has read anything, cannot deadlock
    # against this one reading stdout.
    threads = [threading.Thread(target=pump_stderr, daemon=True)]
    if stdin is not None:
        threads.append(threading.Thread(target=feed_stdin, daemon=True))
    for t in threads:
        t.start()

    expired = threading.Event()
    timer: threading.Timer | None = None
    if timeout is not None:
        def expire() -> None:
            expired.set()
            _end(proc)
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()

    try:
        out = proc.stdout.read()
        proc.wait()
    except BaseException:
        # Interrupted while waiting (a Ctrl-C in a notebook): the tool must
        # not outlive the call that started it.
        _end(proc)
        raise
    finally:
        if timer is not None:
            timer.cancel()
    # The pumps end at EOF, which arrives when the tool dies; the bound is
    # for a tool that leaves a child holding its stderr, which none does today.
    for t in threads:
        t.join(timeout=5)

    if job is not None:
        job._detach()
        if job.cancelled:
            raise Cancelled(f"{tool}: cancelled")
    # A timer that fires as the tool exits on its own is not a timeout: a
    # process the timer ended never exits 0.
    if expired.is_set() and proc.returncode != 0:
        raise LoomError(f"{tool} timed out after {_seconds(timeout)}:\n" + "\n".join(tail))
    if proc.returncode != 0:
        what = " ".join([tool, *args])
        raise LoomError(f"{what} failed ({proc.returncode}):\n" + "\n".join(tail))
    return out


def _seconds(value: float | None) -> str:
    """A duration for a sentence: seconds under two minutes, minutes above."""
    if value is None:
        return "no limit"
    if value < 120:
        return f"{value:g} s"
    minutes = value / 60
    return f"{minutes:g} minutes"


def run(tool: str, graph: Graph | bytes, *args: str,
        timeout: float | None = TIMEOUT) -> Graph:
    """Run one graph-to-graph tool (topo, loom, octi)."""
    payload = graph if isinstance(graph, bytes) else json.dumps(graph).encode()
    return json.loads(execute(tool, payload, args=args, timeout=timeout))


def pipeline(graph: Graph | bytes, *stages: str | tuple[str, ...],
             timeout: float | None = TIMEOUT) -> Graph:
    """Chain graph-to-graph tools: ``pipeline(g, "topo", "loom", ("octi", "-b", "orthoradial"))``."""
    payload = graph if isinstance(graph, bytes) else json.dumps(graph).encode()
    for stage in stages:
        tool, *args = (stage,) if isinstance(stage, str) else stage
        payload = execute(tool, payload, args=args, timeout=timeout)
    return json.loads(payload)


def gtfs2graph(feed_zip: Path, *args: str, timeout: float | None = TIMEOUT) -> Graph:
    """Convert a GTFS zip into a LOOM line graph."""
    return json.loads(execute("gtfs2graph", None, args=args, timeout=timeout, feed=feed_zip))


def transitmap(graph: Graph | bytes, *args: str, timeout: float | None = TIMEOUT) -> str:
    """Render a line graph to SVG. The Docker image has the tool; the app's binaries do not."""
    payload = graph if isinstance(graph, bytes) else json.dumps(graph).encode()
    return execute("transitmap", payload, args=args, timeout=timeout).decode()


def help_text(tool: str) -> str:
    """Capture a tool's -h output; the READMEs omit most options."""
    try:
        proc = subprocess.run(backend().command(tool, ["-h"]), capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=60, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise LoomError(f"{tool} -h timed out after {_seconds(60)}") from exc
    return (proc.stdout + proc.stderr).decode(errors="replace")
