"""schematic.loom: two backends behind one runner.

The native backend is exercised with stand-in tools -- small Python scripts
in a scratch directory named ``topo``, ``gtfs2graph`` and so on -- so the
timeout, the cancel, the stderr tail, the stdin rule and the unpacked feed
are all asserted without LOOM, Docker or a feed. What needs the real thing
(the Docker path's timeout, the native binaries against the image) skips
where it is missing, and says so.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from schematic import config, feeds, loom

posix_only = pytest.mark.skipif(os.name == "nt", reason="the stand-in tools are shebang scripts")

STAND_INS = {
    # Prints its pid, then sleeps, so a test can see it end.
    "sleepy": """\
import os, sys, time
sys.stderr.write(f"pid {os.getpid()}\\n"); sys.stderr.flush()
time.sleep(30)
""",
    # Echoes the graph it was given, with what it saw of stdin and argv.
    "echo": """\
import json, sys
payload = sys.stdin.read()
graph = json.loads(payload) if payload else {"type": "FeatureCollection", "features": []}
graph["stdin_bytes"] = len(payload)
graph["argv"] = sys.argv[1:]
sys.stdout.write(json.dumps(graph))
""",
    # A gtfs2graph that reports the directory it was handed and what stdin held.
    "reader": """\
import json, os, sys
target = sys.argv[-1]
sys.stdout.write(json.dumps({
    "type": "FeatureCollection", "features": [],
    "argv": sys.argv[1:],
    "is_dir": os.path.isdir(target),
    "listing": sorted(os.listdir(target)) if os.path.isdir(target) else None,
    "stdin_bytes": len(sys.stdin.read()),
}))
""",
    # Twenty lines of stderr, then a failure.
    "failing": """\
import sys
for i in range(20):
    sys.stderr.write(f"line {i}\\n")
sys.exit(3)
""",
}


def install(bin_dir: Path, tool: str, stand_in: str) -> Path:
    path = bin_dir / tool
    path.write_text(f"#!{sys.executable}\n" + STAND_INS[stand_in])
    path.chmod(0o755)
    return path


@pytest.fixture
def native(tmp_path, monkeypatch):
    """A native backend over a scratch directory, with a space in its path
    because the app's user-data folder may well have one."""
    bin_dir = tmp_path / "loom bin"
    bin_dir.mkdir()
    monkeypatch.setenv(loom.BIN_ENV, str(bin_dir))
    return bin_dir


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# --------------------------------------------------------------- selection

def test_docker_unless_the_variable_names_a_directory(monkeypatch, tmp_path):
    monkeypatch.delenv(loom.BIN_ENV, raising=False)
    assert loom.backend().name == "docker"
    monkeypatch.setenv(loom.BIN_ENV, "")
    assert loom.backend().name == "docker"
    monkeypatch.setenv(loom.BIN_ENV, str(tmp_path))
    chosen = loom.backend()
    assert chosen.name == "native"
    assert isinstance(chosen, loom.NativeBackend)
    assert chosen.bin_dir == tmp_path


def test_the_commit_is_told_not_asked(monkeypatch):
    monkeypatch.delenv(loom.COMMIT_ENV, raising=False)
    assert loom.commit() is None
    monkeypatch.setenv(loom.COMMIT_ENV, " 1e4757838104d1e4d22186c9b77d5fc4b98681a0 ")
    assert loom.commit() == "1e4757838104d1e4d22186c9b77d5fc4b98681a0"
    monkeypatch.setenv(loom.COMMIT_ENV, "")
    assert loom.commit() is None


def test_docker_commands_mount_the_feed_and_name_the_image(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    b = loom.DockerBackend()
    assert b.command("topo", ["-x"]) == ["docker", "run", "--rm", "-i", "--init", loom.IMAGE,
                                         "topo", "-x"]
    feed = Path("/feeds/la.normalized.zip")
    cmd = b.feed_command(feed, ["-m", "all"])
    assert cmd[-5:] == [loom.IMAGE, "gtfs2graph", "-m", "all", "/feed/la.normalized.zip"]
    assert "-v" in cmd and cmd[cmd.index("-v") + 1].endswith(":/feed:ro")
    # tini at PID 1, or a cancel's SIGTERM never reaches the tool.
    assert "--init" in cmd


def test_docker_missing_is_a_loom_error(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(loom.LoomError, match="docker not found"):
        loom.DockerBackend().command("topo")


def test_native_finds_the_tool_or_its_exe_and_refuses_a_missing_one(tmp_path):
    b = loom.NativeBackend(tmp_path)
    (tmp_path / "topo").write_text("")
    (tmp_path / "octi.exe").write_text("")
    assert b.command("topo", ["-a"]) == [str(tmp_path / "topo"), "-a"]
    assert b.command("octi") == [str(tmp_path / "octi.exe")]
    with pytest.raises(loom.LoomError, match="has no loom"):
        b.command("loom")
    with pytest.raises(loom.LoomError, match=loom.BIN_ENV):
        b.command("transitmap")


# ----------------------------------------------------------- the runner

@posix_only
def test_a_graph_tool_gets_its_payload_on_stdin_and_nothing_else_gets_any(native):
    install(native, "topo", "echo")
    out = loom.run("topo", {"type": "FeatureCollection", "features": []}, "-x", timeout=10)
    assert out["stdin_bytes"] > 0
    assert out["argv"] == ["-x"]
    # No payload means no stdin at all: the parent's is the server's protocol.
    install(native, "gtfs2graph", "reader")
    zipped = native.parent / "feed with space.normalized.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr("routes.txt", "route_id\n1\n")
    out = json.loads(loom.execute("gtfs2graph", None, feed=zipped, timeout=10))
    assert out["stdin_bytes"] == 0


@posix_only
def test_native_gtfs2graph_is_handed_the_feed_unpacked(native):
    install(native, "gtfs2graph", "reader")
    zipped = native.parent / "la with space.normalized.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr("routes.txt", "route_id,route_short_name\n1,A\n")
        zf.writestr("stops.txt", "stop_id\n1\n")
    out = loom.gtfs2graph(zipped, "-m", "all", timeout=10)
    directory = native.parent / "la with space.normalized"
    assert out["argv"] == ["-m", "all", str(directory)]
    assert out["is_dir"] is True
    assert out["listing"] == ["routes.txt", "stops.txt"]

    # Unpacked once: the directory is reused while it is newer than the zip.
    stamp = directory.stat().st_mtime_ns
    loom.gtfs2graph(zipped, timeout=10)
    assert directory.stat().st_mtime_ns == stamp

    # A newer zip is unpacked again, and the old contents do not linger.
    time.sleep(0.05)
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr("routes.txt", "route_id\n2\n")
    os.utime(zipped, None)
    out = loom.gtfs2graph(zipped, timeout=10)
    assert out["listing"] == ["routes.txt"]
    # Nothing of the scratch is left, and its name is never normalize()'s .tmp,
    # which is a file there and would make the next normalize fail.
    assert not (native.parent / "la with space.normalized.unpacking").exists()
    assert not (native.parent / "la with space.normalized.tmp").exists()


@posix_only
def test_two_unpacks_of_one_feed_at_once_both_see_the_whole_feed(native):
    install(native, "gtfs2graph", "reader")
    zipped = native.parent / "shared.normalized.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        for i in range(40):
            zf.writestr(f"table{i:02d}.txt", "x\n" * 2000)
    results: list[dict] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            results.append(loom.gtfs2graph(zipped, timeout=30))
        except BaseException as exc:  # noqa: BLE001 - the test reads it
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert all(r["listing"] == [f"table{i:02d}.txt" for i in range(40)] for r in results)


@posix_only
def test_a_timeout_ends_the_tool_and_names_it(native):
    install(native, "topo", "sleepy")
    lines: list[str] = []
    started = time.monotonic()
    with pytest.raises(loom.LoomError, match=r"topo timed out after 1 s") as caught:
        loom.execute("topo", b"{}", timeout=1.0, on_stderr=lines.append)
    assert time.monotonic() - started < 8
    pid = int(next(line for line in lines if line.startswith("pid ")).split()[1])
    assert not alive(pid), "the tool outlived its timeout"
    assert "pid" in str(caught.value), "the tail travels with the error"


@posix_only
def test_a_cancel_ends_the_tool_within_a_second_and_raises_cancelled(native):
    install(native, "loom", "sleepy")
    job = loom.Job()
    outcome: dict[str, object] = {}

    def work() -> None:
        try:
            with loom.cancellable(job):
                loom.run("loom", b"{}", timeout=30)
        except BaseException as exc:  # noqa: BLE001 - the test reads it
            outcome["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    deadline = time.monotonic() + 10
    while job.running is None and time.monotonic() < deadline:
        time.sleep(0.02)
    proc = job.running
    assert proc is not None, "the tool never started"
    started = time.monotonic()
    job.cancel()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert time.monotonic() - started < 1.5
    assert isinstance(outcome.get("error"), loom.Cancelled)
    assert proc.poll() is not None and not alive(proc.pid)
    assert job.running is None

    # Cancelled before it started: nothing is run at all.
    with pytest.raises(loom.Cancelled, match="before it started"):
        with loom.cancellable(job):
            loom.run("loom", b"{}")


@posix_only
def test_a_failing_tool_reports_the_tail_and_the_code(native):
    install(native, "octi", "failing")
    seen: list[str] = []
    with pytest.raises(loom.LoomError) as caught:
        loom.execute("octi", b"{}", args=["-b", "orthoradial"], timeout=10, on_stderr=seen.append)
    message = str(caught.value)
    assert message.startswith("octi -b orthoradial failed (3):")
    assert "line 19" in message and "line 5" in message and "line 4" not in message
    assert seen == [f"line {i}" for i in range(20)]


@posix_only
def test_the_job_log_hears_every_stderr_line(native):
    install(native, "octi", "failing")
    heard: list[str] = []
    job = loom.Job(log=heard.append)
    with pytest.raises(loom.LoomError):
        with loom.cancellable(job):
            loom.run("octi", b"{}", timeout=10)
    assert heard == [f"line {i}" for i in range(20)]


# --------------------------------------------------------- the real thing

KEY = "la-metro-rail"
SOURCE_ZIP = config.feeds_dir() / f"{KEY}.normalized.zip"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", loom.IMAGE],
                           capture_output=True, stdin=subprocess.DEVNULL)
    return probe.returncode == 0


def _native_ready() -> bool:
    value = os.environ.get(loom.BIN_ENV, "")
    return bool(value) and all((Path(value) / t).exists() or (Path(value) / f"{t}.exe").exists()
                               for t in loom.TOOLS)


needs_docker = pytest.mark.skipif(not (_docker_ready() and SOURCE_ZIP.exists()),
                                  reason="needs docker, the loom image and the cached LA feed")
needs_both = pytest.mark.skipif(
    not (_docker_ready() and _native_ready() and SOURCE_ZIP.exists()),
    reason=f"needs docker, the image, the cached LA feed and {loom.BIN_ENV} naming the binaries")


def _containers() -> list[str]:
    out = subprocess.run(["docker", "ps", "-q", "--filter", f"ancestor={loom.IMAGE}"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    return out.split()


needs_image = pytest.mark.skipif(not _docker_ready(), reason="needs docker and the loom image")


@needs_image
def test_a_timeout_on_the_docker_backend_ends_the_tool_in_the_container(monkeypatch):
    """Something that never finishes on its own, so the test can tell a
    signal that reached the tool from a container that ran to its end."""
    monkeypatch.delenv(loom.BIN_ENV, raising=False)
    started = time.monotonic()
    with pytest.raises(loom.LoomError, match="sleep timed out after 1 s"):
        loom.execute("sleep", None, args=["30"], timeout=1.0)
    deadline = time.monotonic() + 8
    while _containers() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _containers(), "the container outlived the client: SIGTERM never reached the tool"
    assert time.monotonic() - started < 10


@needs_image
def test_a_cancel_on_the_docker_backend_ends_the_tool_in_the_container(monkeypatch):
    monkeypatch.delenv(loom.BIN_ENV, raising=False)
    job = loom.Job()
    outcome: dict[str, object] = {}

    def work() -> None:
        try:
            with loom.cancellable(job):
                loom.execute("sleep", None, args=["30"], timeout=60)
        except BaseException as exc:  # noqa: BLE001 - the test reads it
            outcome["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    deadline = time.monotonic() + 30
    while not _containers() and time.monotonic() < deadline:
        time.sleep(0.1)
    started = time.monotonic()
    job.cancel()
    thread.join(timeout=15)
    assert isinstance(outcome.get("error"), loom.Cancelled)
    while _containers() and time.monotonic() < started + 8:
        time.sleep(0.1)
    assert not _containers()
    assert time.monotonic() - started < 10


def _node_key(feature: dict) -> str:
    """Identity that survives renumbering: the station, else the position.
    LOOM writes node ids that are pointer addresses, different on every run."""
    props = feature.get("properties") or {}
    for field in ("station_id", "station_label"):
        if props.get(field) not in (None, ""):
            return f"{field}={props[field]}"
    lon, lat = feature["geometry"]["coordinates"][:2]
    return f"at={lon:.6f},{lat:.6f}"


def _as_graph(graph: dict) -> dict:
    """What the pipeline depends on: the nodes, the edges by their endpoints,
    and the ordered line labels along each edge."""
    nodes = [f for f in graph["features"] if f["geometry"]["type"] == "Point"]
    edges = [f for f in graph["features"] if f["geometry"]["type"] != "Point"]
    by_id = {(f.get("properties") or {}).get("id"): _node_key(f) for f in nodes}
    return {
        "nodes": sorted(_node_key(f) for f in nodes),
        "edges": sorted(
            (tuple(sorted((by_id.get(f["properties"].get("from")),
                           by_id.get(f["properties"].get("to"))))),
             tuple(line["label"] for line in f["properties"].get("lines", [])))
            for f in edges),
    }


@needs_both
def test_native_binaries_against_the_image_on_la(monkeypatch, tmp_path):
    """gtfs2graph must agree as a graph (never as bytes: the ids are pointer
    addresses); the later stages are compared the same way and the
    differences printed rather than asserted, because topo is not
    reproducible across builds (the app's ADR-019). Run with -s to read it."""
    shutil.copy(SOURCE_ZIP, tmp_path / SOURCE_ZIP.name)
    feed = tmp_path / SOURCE_ZIP.name
    outputs: dict[str, dict[str, dict]] = {}
    for name in ("native", "docker"):
        if name == "docker":
            monkeypatch.delenv(loom.BIN_ENV)
        graph = loom.gtfs2graph(feed, "-m", feeds.FEEDS[KEY].mode)
        outputs[name] = {"gtfs2graph": graph}
        payload = graph
        for tool in ("topo", "loom", "octi"):
            payload = loom.run(tool, payload)
            outputs[name][tool] = payload
    for tool in ("gtfs2graph", "topo", "loom", "octi"):
        n, d = _as_graph(outputs["native"][tool]), _as_graph(outputs["docker"][tool])
        print(f"{tool}: same graph={n == d}; native {len(n['nodes'])} nodes "
              f"{len(n['edges'])} edges, docker {len(d['nodes'])} nodes {len(d['edges'])} "
              f"edges; nodes only native={sorted(set(n['nodes']) - set(d['nodes']))[:5]}; "
              f"only docker={sorted(set(d['nodes']) - set(n['nodes']))[:5]}; "
              f"edges differing={len(set(n['edges']) ^ set(d['edges']))}")
    assert _as_graph(outputs["native"]["gtfs2graph"]) == _as_graph(outputs["docker"]["gtfs2graph"])
