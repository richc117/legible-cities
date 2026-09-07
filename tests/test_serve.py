"""schematic.serve: the engine over JSON-RPC, driven by a fake client.

Two clients. The in-process one feeds messages straight into the endpoint and
collects what it sends, which tests the methods, the errors and the schema
without a process. The pipe one runs ``python -m schematic.serve`` and speaks
Content-Length frames to it, which tests the transport, the handshake,
progress, cancellation and shutdown as the desktop app will see them.

Everything that runs LOOM needs Docker and the image, and the LA feed from the
developer's cache (copied into a scratch home so nothing here touches data/).
Those tests skip where either is missing; the rest always run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import jsonschema
import pandas as pd
import pytest
from jsonschema import Draft202012Validator

from schematic import __version__, config, feeds, loom, schedule, serve

SCHEMA = serve.schema()
KEY = "la-metro-rail"
DATE = "2026-09-11"

SOURCE_ZIPS = [config.feeds_dir() / f"{KEY}{suffix}.zip" for suffix in ("", ".normalized")]


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", loom.IMAGE],
                           capture_output=True, stdin=subprocess.DEVNULL)
    return probe.returncode == 0


needs_loom = pytest.mark.skipif(
    not (_docker_ready() and all(z.exists() for z in SOURCE_ZIPS)),
    reason="needs docker, the loom image and the cached LA feed")


def check(instance, name: str) -> None:
    """Validate ``instance`` against one named definition of the protocol schema,
    with formats asserted: ``format: date`` is part of the contract."""
    jsonschema.validate(instance, {"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]},
                        cls=Draft202012Validator,
                        format_checker=Draft202012Validator.FORMAT_CHECKER)


def invalid(instance, name: str) -> bool:
    try:
        check(instance, name)
    except jsonschema.ValidationError:
        return True
    return False


def loom_containers() -> list[str]:
    out = subprocess.run(["docker", "ps", "-q", "--filter", f"ancestor={loom.IMAGE}"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    return out.split()


def wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def home(tmp_path_factory):
    """A scratch SCHEMATIC_HOME holding the LA feed, shared across the module
    so LOOM runs for LA once (about ten seconds) rather than per test."""
    root = tmp_path_factory.mktemp("home")
    (root / "data" / "feeds").mkdir(parents=True)
    for z in SOURCE_ZIPS:
        if z.exists():
            shutil.copy(z, root / "data" / "feeds" / z.name)
    patch = pytest.MonkeyPatch()
    patch.setenv(config.ENV, str(root))
    yield root
    patch.undo()


class Client:
    """In-process: requests go into the endpoint, its messages land in a list."""

    def __init__(self) -> None:
        self.out: list[dict] = []
        self.cv = threading.Condition()
        self.endpoint = serve.EngineEndpoint(self._consume)
        self.n = 0

    def _consume(self, message: dict) -> None:
        with self.cv:
            self.out.append(message)
            self.cv.notify_all()

    def send(self, method: str, params=None) -> int:
        self.n += 1
        message = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            message["params"] = params
        self.endpoint.consume(message)
        return self.n

    def notify(self, method: str, params) -> None:
        self.endpoint.consume({"jsonrpc": "2.0", "method": method, "params": params})

    def wait(self, msg_id: int, timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        with self.cv:
            while True:
                for m in self.out:
                    if m.get("id") == msg_id and "method" not in m:
                        return m
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"no response to request {msg_id}")
                self.cv.wait(left)

    def call(self, method: str, params=None, timeout: float = 120) -> dict:
        return self.wait(self.send(method, params), timeout)

    def notifications(self, method: str, msg_id: int) -> list[dict]:
        return [m["params"] for m in self.out
                if m.get("method") == method and m["params"]["id"] == msg_id]


@pytest.fixture
def client():
    c = Client()
    yield c
    c.endpoint.close()


class PipeClient:
    """Over pipes: ``python -m schematic.serve`` as the app will run it."""

    def __init__(self, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "schematic.serve"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.watchdog = threading.Timer(300, self.proc.kill)
        self.watchdog.start()
        self.n = 0
        self.notes: list[dict] = []

    def write(self, message: dict) -> None:
        body = json.dumps(message).encode()
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.proc.stdin.flush()

    def read(self) -> dict:
        headers: dict[str, str] = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise EOFError("the server closed stdout")
            if line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode().partition(":")
            headers[name.strip().lower()] = value.strip()
        return json.loads(self.proc.stdout.read(int(headers["content-length"])))

    def send(self, method: str, params=None) -> int:
        self.n += 1
        message = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            message["params"] = params
        self.write(message)
        return self.n

    def response(self, msg_id: int) -> dict:
        while True:
            message = self.read()
            if "method" in message:
                self.notes.append(message)
                continue
            if message.get("id") == msg_id:
                return message

    def call(self, method: str, params=None) -> dict:
        return self.response(self.send(method, params))

    def notifications(self, method: str, msg_id: int) -> list[dict]:
        return [m["params"] for m in self.notes
                if m["method"] == method and m["params"]["id"] == msg_id]

    def finish(self, timeout: float = 15) -> tuple[int, str]:
        """Close stdin, wait for the process to exit; its code and its stderr."""
        try:
            _, err = self.proc.communicate(timeout=timeout)
        finally:
            self.watchdog.cancel()
        return self.proc.returncode, err.decode(errors="replace")


@pytest.fixture
def pipes(home):
    env = {**os.environ, config.ENV: str(home), "SCHEMATIC_LOG": "info"}
    c = PipeClient(env)
    yield c
    if c.proc.poll() is None:
        c.proc.kill()
        c.proc.wait()
    c.watchdog.cancel()


# -------------------------------------------------------------------- schema

def test_schema_is_a_valid_schema_and_names_every_method():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["protocol"] == serve.PROTOCOL
    endpoint = serve.EngineEndpoint(lambda m: None)
    assert sorted(SCHEMA["methods"]) == sorted(endpoint.methods)
    endpoint.close()
    assert sorted(SCHEMA["notifications"]) == ["$/cancelRequest", "job/log", "job/progress"]


def test_schema_flag_prints_the_same_schema(capsys):
    assert serve.main(["--schema"]) == 0
    assert json.loads(capsys.readouterr().out) == SCHEMA


def test_patterns_agree_with_the_schema():
    defs = SCHEMA["$defs"]
    assert serve.KEY_PATTERN.pattern == defs["FeedKey"]["pattern"]
    assert serve.TOKEN_PATTERN.pattern == defs["Token"]["pattern"]
    assert serve.DATE_PATTERN.pattern == defs["ServiceDate"]["pattern"]
    assert set(serve.KIND_BY_MODULE.values()) | {"engine", "io", "params"} \
        == set(defs["ErrorData"]["properties"]["kind"]["enum"])


def test_every_registered_key_is_a_valid_feed_key():
    for key in feeds.FEEDS:
        check(key, "FeedKey")


# ------------------------------------------------------------ short methods

def test_engine_info_is_the_handshake(client):
    response = client.call("engine.info")
    info = response["result"]
    check(info, "EngineInfo")
    assert info["engine"] == __version__
    assert info["protocol"] == 1
    assert info["home"] == str(config.home())
    assert info["loom"]["backend"] == "docker"


def test_no_parameter_methods_refuse_parameters(client):
    for method in ("engine.info", "engine.shutdown"):
        error = client.call(method, {"x": 1})["error"]
        assert error["code"] == -32602
        check(error["data"], "ErrorData")
        assert error["data"]["kind"] == "params"
    assert client.call("engine.info", {})["result"]["protocol"] == 1


def test_unknown_method_is_method_not_found(client):
    assert client.call("feeds.list")["error"]["code"] == -32601


def test_unknown_feed_is_a_feed_error_with_a_hint(client):
    error = client.call("graph.build", {"key": "not-a-feed"})["error"]
    assert error["code"] == -32000
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "feed"
    assert "not a registered feed" in error["data"]["hint"]


def test_map_build_without_a_date_is_a_schema_error_not_a_default(client):
    error = client.call("map.build", {"key": KEY})["error"]
    assert error["code"] == -32602
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "params"
    assert "date is required" in error["data"]["hint"]
    assert invalid({"key": KEY}, "MapBuildParams")


BAD_PARAMS = [
    ("graph.build", None, "GraphBuildParams"),
    ("graph.build", [], "GraphBuildParams"),
    ("graph.build", {}, "GraphBuildParams"),
    ("graph.build", {"key": "../etc"}, "GraphBuildParams"),
    ("graph.build", {"key": "LA"}, "GraphBuildParams"),
    ("graph.build", {"key": KEY, "force": "yes"}, "GraphBuildParams"),
    ("graph.build", {"key": KEY, "mode": "tram"}, "GraphBuildParams"),
    ("map.build", {"key": KEY, "date": "11/09/2026"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "date": "2026-13-40"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "date": 20260911}, "MapBuildParams"),
    ("map.build", {"key": KEY, "date": DATE, "out": "../x"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "date": DATE, "out": ".hidden"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "date": DATE, "out": "a/b"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "date": DATE, "width": 0}, "MapBuildParams"),
    ("map.build", {"key": KEY, "date": DATE, "width": "wide"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "date": DATE, "line_order": "A,B"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "date": DATE, "style": {}}, "MapBuildParams"),
]


@pytest.mark.parametrize("method,params,definition", BAD_PARAMS)
def test_hand_validation_refuses_what_the_schema_refuses(client, method, params, definition):
    """The server validates by hand (jsonschema is a dev dependency); the two
    must agree, or the app's generated types would promise what the server
    refuses -- or the reverse."""
    error = client.call(method, params)["error"]
    assert error["code"] == -32602, error
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "params"
    assert invalid(params, definition)


def test_calendar_check_goes_past_the_pattern(client):
    # 2026-02-30 matches the pattern and is not a day.
    error = client.call("map.build", {"key": KEY, "date": "2026-02-30"})["error"]
    assert error["code"] == -32602
    assert "not a calendar day" in error["data"]["hint"]


GOOD_PARAMS = [
    ("GraphBuildParams", {"key": KEY}),
    ("GraphBuildParams", {"key": KEY, "force": True}),
    ("MapBuildParams", {"key": KEY, "date": DATE}),
    ("MapBuildParams", {"key": KEY, "date": DATE, "out": "p1", "width": 900,
                        "line_order": ["A", "B"], "force": False}),
]


@pytest.mark.parametrize("definition,params", GOOD_PARAMS)
def test_the_schema_accepts_what_the_server_accepts(definition, params):
    check(params, definition)


# -------------------------------------------------------------------- errors

def test_errors_are_classified_by_the_module_that_wrote_the_sentence():
    tables = {"trips": pd.DataFrame({"service_id": [], "route_id": []})}
    try:
        schedule.busiest_weekday(tables)
    except ValueError as exc:
        error = serve.classify(exc)
    check(error.data, "ErrorData")
    assert error.code == -32000
    assert error.data["kind"] == "schedule"
    assert error.data["hint"].startswith("feed has neither calendar.txt")
    assert "schedule.py:" in error.data["detail"]

    try:
        raise loom.LoomError("docker not found on PATH")
    except loom.LoomError as exc:
        assert serve.classify(exc).data["kind"] == "loom"

    try:
        raise FileNotFoundError(2, "No such file or directory", "/nowhere")
    except OSError as exc:
        error = serve.classify(exc)
    assert error.data["kind"] == "io"
    assert error.data["hint"] == "No such file or directory: /nowhere"

    try:
        {}["k"]
    except KeyError as exc:
        error = serve.classify(exc)
    assert error.data["kind"] == "engine"
    assert "did not expect" in error.data["hint"]
    assert "KeyError" in error.data["detail"]


def test_cancel_for_an_unknown_request_is_ignored(client, caplog):
    client.notify("$/cancelRequest", {"id": 999})
    assert client.out == []


# ---------------------------------------------------------------- with LOOM

@needs_loom
def test_graph_build_reports_four_stages(client, home):
    msg_id = client.send("graph.build", {"key": KEY})
    response = client.wait(msg_id)
    assert "result" in response, response
    result = response["result"]
    check(result, "GraphBuildResult")

    progress = client.notifications("job/progress", msg_id)
    assert [p["stage"] for p in progress] == ["gtfs2graph", "topo", "loom", "octi"]
    assert [p["fraction"] for p in progress] == [0.25, 0.5, 0.75, 1.0]
    for p in progress:
        check(p, "JobProgress")
        assert "nodes" in p["message"]

    assert result["stages"]["octi"]["stations"] > 50
    assert result["stages"]["octi"]["octilinear"] > 0.95
    assert set(result["stages"]["octi"]["lines"]) >= {"A", "B", "C", "D", "E"}
    for stage, path in result["paths"].items():
        assert Path(path).is_file()
        assert Path(path).parent == home / "data" / "graphs" / KEY
    assert client.endpoint.jobs == {}


@needs_loom
def test_cancel_ends_the_loom_process_and_leaves_the_cache_alone(client, home):
    cached = home / "data" / "graphs" / KEY / "00_gtfs2graph.json"
    before = cached.stat().st_mtime_ns if cached.exists() else None

    msg_id = client.send("graph.build", {"key": KEY, "force": True})
    wait_for(lambda: msg_id in client.endpoint.jobs
             and client.endpoint.jobs[msg_id].running is not None, 30, "gtfs2graph to start")
    proc = client.endpoint.jobs[msg_id].running
    time.sleep(1.0)  # well inside gtfs2graph, which takes about ten seconds
    client.notify("$/cancelRequest", {"id": msg_id})

    response = client.wait(msg_id, timeout=30)
    assert response["error"]["code"] == -32800, response
    assert proc.poll() is not None, "the docker client is still running"
    assert client.endpoint.jobs == {}
    wait_for(lambda: not loom_containers(), 15, "the loom container to go")
    if before is not None:
        assert cached.stat().st_mtime_ns == before, "a cancelled stage overwrote the cache"


@needs_loom
def test_a_mode_that_matches_nothing_returns_the_hint(client, home, monkeypatch):
    for z in SOURCE_ZIPS:
        shutil.copy(z, home / "data" / "feeds" / z.name.replace(KEY, "la-ferry"))
    monkeypatch.setitem(feeds.FEEDS, "la-ferry",
                        replace(feeds.FEEDS[KEY], key="la-ferry", mode="ferry"))
    error = client.call("graph.build", {"key": "la-ferry"}, timeout=180)["error"]
    assert error["code"] == -32000
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "feed"
    assert "matched no routes" in error["data"]["hint"]
    assert "'ferry'" in error["data"]["hint"]


@needs_loom
def test_map_build_writes_under_out_and_reports_diagnostics(client, home):
    msg_id = client.send("map.build", {"key": KEY, "date": DATE, "out": "p1"})
    response = client.wait(msg_id)
    assert "result" in response, response
    result = response["result"]
    check(result, "MapBuildResult")

    assert result["date"] == DATE
    for kind, path in result["files"].items():
        assert Path(path).is_file(), kind
        assert Path(path).parent == home / "out" / "p1"
    assert f"Friday 11 September 2026" in result["summary"]

    d = result["diagnostics"]
    assert d["stations"] == result["diagnostics"]["stops"]["by"]["station_id"] \
        or d["stations"] > 0
    assert d["trips"]["total"] > 1000
    assert d["trips"]["paths"] > 0
    assert d["stops"]["matched"] == d["stops"]["total"]
    assert 0.9 < d["octilinear"] <= 1.0
    assert d["peak_concurrent"] > 0

    stages = [p["stage"] for p in client.notifications("job/progress", msg_id)]
    assert stages == ["gtfs2graph", "topo", "loom", "octi",
                      "schedule", "render", "animate", "write"]
    fractions = [p["fraction"] for p in client.notifications("job/progress", msg_id)]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0


# ----------------------------------------------------------------- over pipes

def test_over_pipes_handshake_errors_and_shutdown(pipes):
    info = pipes.call("engine.info")["result"]
    check(info, "EngineInfo")
    assert info["protocol"] == 1

    error = pipes.call("map.build", {"key": KEY})["error"]
    assert error["code"] == -32602 and error["data"]["kind"] == "params"
    assert pipes.call("no.such")["error"]["code"] == -32601

    assert pipes.call("engine.shutdown")["result"] == {"ok": True}
    code, err = pipes.finish()
    assert code == 0, err
    assert f"engine {__version__}, protocol 1" in err
    assert "Traceback" not in err, err


def test_over_pipes_eof_ends_the_server(pipes):
    """No shutdown request: the app died, or closed the pipe. The server goes."""
    assert pipes.call("engine.info")["result"]["protocol"] == 1
    code, err = pipes.finish()
    assert code == 0, err
    assert "Traceback" not in err, err


@needs_loom
def test_over_pipes_progress_and_cancellation(pipes, home):
    """The acceptance case: handshake, graph.build for LA with four progress
    notifications, then a second run cancelled mid-stage with no process
    left behind."""
    assert pipes.call("engine.info")["result"]["engine"] == __version__

    msg_id = pipes.send("graph.build", {"key": KEY})
    response = pipes.response(msg_id)
    assert "result" in response, response
    check(response["result"], "GraphBuildResult")
    progress = pipes.notifications("job/progress", msg_id)
    assert [p["stage"] for p in progress] == ["gtfs2graph", "topo", "loom", "octi"]
    for p in progress:
        check(p, "JobProgress")

    second = pipes.send("graph.build", {"key": KEY, "force": True})
    wait_for(loom_containers, 30, "the loom container to start")
    time.sleep(1.0)
    pipes.write({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": second}})
    response = pipes.response(second)
    assert response["error"]["code"] == -32800, response
    wait_for(lambda: not loom_containers(), 15, "the loom container to go")

    assert pipes.call("engine.shutdown")["result"] == {"ok": True}
    code, err = pipes.finish()
    assert code == 0, err
    assert "cancelling request" in err
    assert not loom_containers()
