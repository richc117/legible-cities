"""schematic.serve: the engine over JSON-RPC, driven by a fake client.

Two clients. The in-process one feeds messages straight into the endpoint and
collects what it sends, which tests the methods, the errors and the schema
without a process. The pipe one runs ``python -m schematic.serve`` and speaks
Content-Length frames to it, which tests the transport, the handshake,
progress, cancellation and shutdown as the desktop app will see them.

Everything that runs LOOM needs Docker and the image, and the LA feed from the
developer's cache (copied into a scratch home so nothing here touches data/).
Those tests skip where either is missing, and when ``SCHEMATIC_LOOM_BIN``
chooses the native backend, which has a test of its own; the rest always run.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pandas as pd
import pytest
from unittest import mock
from jsonschema import Draft202012Validator

from pylsp_jsonrpc.exceptions import JsonRpcRequestCancelled
from test_colors import _graph

from schematic import (__version__, config, diagnostics, export, feeds, loom, pipeline, schedule,
                       serve, thumbnail)

SCHEMA = serve.schema()
KEY = "la-metro-rail"
DATE = "2026-09-11"

SOURCE_ZIPS = [config.feeds_dir() / f"{KEY}{suffix}.zip" for suffix in ("", ".normalized")]
# Mexico City's feed too, for the service day: its window has expired, which
# is the case the anchor rule exists for. Copied when cached, skipped when not.
CDMX = "cdmx-metro"
CDMX_ZIPS = [config.feeds_dir() / f"{CDMX}{suffix}.zip" for suffix in ("", ".normalized")]


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", loom.IMAGE],
                           capture_output=True, stdin=subprocess.DEVNULL)
    return probe.returncode == 0


# These drive LOOM through Docker and watch its containers, so they need the
# Docker backend to be the one in use, not only Docker to exist: with
# SCHEMATIC_LOOM_BIN set the engine runs the binaries and no container ever
# starts. The native backend has its own test below.
needs_loom = pytest.mark.skipif(
    not (_docker_ready() and all(z.exists() for z in SOURCE_ZIPS))
    or bool(os.environ.get(loom.BIN_ENV)),
    reason=f"needs docker, the loom image and the cached LA feed, with {loom.BIN_ENV} unset")

needs_native = pytest.mark.skipif(
    not (os.environ.get(loom.BIN_ENV) and all(z.exists() for z in SOURCE_ZIPS)),
    reason=f"needs {loom.BIN_ENV} naming the native binaries and the cached LA feed")

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg on PATH")


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
    for z in SOURCE_ZIPS + CDMX_ZIPS:
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


def test_the_schema_names_both_thumbnails_as_files_every_map_build_answers():
    """Issue 51: ``files`` grew by two required keys, the ones the engine
    answers (``thumbnail.files_for``), beside the three it always had."""
    files = SCHEMA["$defs"]["MapBuildResult"]["properties"]["files"]
    assert set(files["properties"]) == {"svg", "html", "positions", *thumbnail.files_for("k")}
    assert set(files["required"]) == set(files["properties"])
    assert files["additionalProperties"] is False


def test_schema_flag_prints_the_same_schema(capsys):
    assert serve.main(["--schema"]) == 0
    assert json.loads(capsys.readouterr().out) == SCHEMA


def test_patterns_agree_with_the_schema():
    defs = SCHEMA["$defs"]
    assert serve.KEY_PATTERN.pattern == defs["FeedKey"]["pattern"]
    assert serve.TOKEN_PATTERN.pattern == defs["Token"]["pattern"]
    assert serve.DATE_PATTERN.pattern == defs["ServiceDate"]["pattern"]
    assert set(serve.KINDS) == set(defs["ErrorData"]["properties"]["kind"]["enum"])
    assert serve.LAYOUT_PATTERN.pattern == defs["LayoutId"]["pattern"]
    assert serve.MODE_PATTERN.pattern == defs["GraphBuildParams"]["properties"]["mode"]["pattern"]
    assert serve.CLOCK_PATTERN.pattern == defs["Clock"]["pattern"]
    assert serve.URL_PATTERN.pattern == defs["PageUrl"]["pattern"]
    assert serve.STEM_PATTERN.pattern == defs["CaptureJob"]["properties"]["stem"]["pattern"]
    assert serve.COLOR_PATTERN.pattern == defs["HexColor"]["pattern"]
    # The app's generated types know every preset and storyboard by name.
    assert defs["PresetName"]["enum"] == list(export.PRESETS)
    assert defs["StoryboardName"]["enum"] == list(export.STORYBOARDS)
    assert defs["View"]["enum"] == list(export.VIEWS)


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
    # Whatever the environment asked for: docker unless SCHEMATIC_LOOM_BIN is set.
    assert info["loom"]["backend"] == loom.backend().name
    assert info["loom"]["commit"] == loom.commit()


def test_engine_info_reports_the_backend_and_the_commit_the_host_passed(client, monkeypatch,
                                                                       tmp_path):
    monkeypatch.setenv(loom.BIN_ENV, str(tmp_path))
    monkeypatch.setenv(loom.COMMIT_ENV, "1e4757838104d1e4d22186c9b77d5fc4b98681a0")
    info = client.call("engine.info")["result"]
    check(info, "EngineInfo")
    assert info["loom"] == {"backend": "native",
                            "commit": "1e4757838104d1e4d22186c9b77d5fc4b98681a0"}
    monkeypatch.delenv(loom.BIN_ENV)
    monkeypatch.setenv(loom.COMMIT_ENV, "  ")
    info = client.call("engine.info")["result"]
    check(info, "EngineInfo")
    assert info["loom"] == {"backend": "docker", "commit": None}


def test_no_parameter_methods_refuse_parameters(client):
    for method in ("engine.info", "engine.shutdown"):
        error = client.call(method, {"x": 1})["error"]
        assert error["code"] == -32602
        check(error["data"], "ErrorData")
        assert error["data"]["kind"] == "params"
    assert client.call("engine.info", {})["result"]["protocol"] == 1


def test_unknown_method_is_method_not_found(client):
    assert client.call("feeds.nothing")["error"]["code"] == -32601


def test_unknown_feed_is_a_feed_error_with_a_hint(client):
    error = client.call("graph.build", {"key": "not-a-feed"})["error"]
    assert error["code"] == -32000
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "feed"
    assert "not a registered feed" in error["data"]["hint"]


NO_LAYOUT = "0" * 64  # well-formed, and stored nowhere


def test_map_build_without_a_date_is_a_schema_error_not_a_default(client):
    error = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT})["error"]
    assert error["code"] == -32602
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "params"
    assert "date is required" in error["data"]["hint"]
    assert invalid({"key": KEY, "layout": NO_LAYOUT}, "MapBuildParams")


def test_map_build_without_a_layout_is_refused_before_anything_runs(client):
    """A map is drawn from a stored layout the caller names; the engine never
    picks one, and never lays out on the way to a map."""
    error = client.call("map.build", {"key": KEY, "date": DATE})["error"]
    assert error["code"] == -32602
    assert error["data"]["kind"] == "params"
    assert "layout is required" in error["data"]["hint"]
    assert invalid({"key": KEY, "date": DATE}, "MapBuildParams")
    assert client.endpoint.jobs == {}


def test_map_build_refuses_a_layout_that_is_not_stored(client, home):
    error = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE},
                        timeout=30)["error"]
    assert error["code"] == -32000, error
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "layout"
    assert "lay the feed out first" in error["data"]["hint"]


def test_map_build_answers_a_thumbnail_pair_beside_the_page(client, tmp_path, monkeypatch):
    """Issue 51: two more files in the answer's ``files``, written into the
    folder of the page, drawn from the graph the build drew with the colours,
    default and order the request carried. In-process, with the pipeline stood
    in for by a hand-made graph, so it runs without a feed or LOOM; the same
    through the real pipeline is the LOOM-gated test below."""
    monkeypatch.setenv(config.ENV, str(tmp_path))
    graph = _graph([[("A", "0072bc"), ("B", None)], [("B", None), ("C", "ff0000")]])
    diag = diagnostics.Diagnostics(
        key="p9", name="Nine", date=dt.date.fromisoformat(DATE), stations=3, junctions=0,
        edges=2, lines=("A", "B", "C"), octilinear=1.0,
        stops=diagnostics.StopMatching(3, 3, 3, 0, 0, ()), trips_total=0, paths=0, unrouted=0,
        skipped_calls=0, borrowed_track=0, labels_dropped=0, peak_concurrent=0)
    asked: dict = {}

    def stood_in(key, **kwargs):
        asked.update(kwargs)
        kwargs["out_dir"].mkdir(parents=True)
        return SimpleNamespace(layout=NO_LAYOUT, date=dt.date.fromisoformat(DATE), graph=graph,
                               diagnostics=lambda: diag)

    monkeypatch.setattr(pipeline, "run", stood_in)
    response = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE,
                                         "out": "p9", "colors": {"A": "#123456"},
                                         "default_color": "#abcdef", "line_order": ["C", "A"]})
    assert "result" in response, response
    files = response["result"]["files"]
    check(response["result"], "MapBuildResult")
    folder = tmp_path / "out" / "p9"
    assert files == {"svg": str(folder / f"{KEY}.svg"), "html": str(folder / f"{KEY}.html"),
                     "positions": str(folder / f"{KEY}.positions.json"),
                     "thumb_dark": str(folder / f"{KEY}-thumb-dark.svg"),
                     "thumb_light": str(folder / f"{KEY}-thumb-light.svg")}
    assert asked["colors"] == {"A": "#123456"} and asked["line_order"] == ["C", "A"]
    for answer in ("thumb_dark", "thumb_light"):
        svg = Path(files[answer]).read_text(encoding="utf-8")
        assert "var(" not in svg and "<text" not in svg
        # The request's colours, default and order, as the page has them.
        assert re.findall(r'<g stroke="(#[0-9a-f]{6})"', svg) == ["#ff0000", "#123456", "#abcdef"]


BAD_PARAMS = [
    ("feeds.service", None, "FeedsServiceParams"),
    ("feeds.service", {}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "anchor": "tomorrow"}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "anchor": 20260910}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "anchor": "2026-02-30"}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "anchor": None}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "lines": "A"}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "date": DATE}, "FeedsServiceParams"),
    ("feeds.add", None, "FeedsAddParams"),
    ("feeds.add", {}, "FeedsAddParams"),
    ("feeds.add", {"source": ""}, "FeedsAddParams"),
    ("feeds.add", {"source": "https://x.test/a.zip", "key": "Bad Key"}, "FeedsAddParams"),
    ("feeds.add", {"source": "https://x.test/a.zip", "mode": "Tram!"}, "FeedsAddParams"),
    ("feeds.add", {"source": "https://x.test/a.zip", "agency": ""}, "FeedsAddParams"),
    ("feeds.add", {"source": "https://x.test/a.zip", "label_pattern": "x"}, "FeedsAddParams"),
    ("feeds.remove", None, "FeedsRemoveParams"),
    ("feeds.remove", {"key": "Nope"}, "FeedsRemoveParams"),
    ("feeds.inspect", {}, "FeedsInspectParams"),
    ("feeds.inspect", {"key": KEY, "anchor": "someday"}, "FeedsInspectParams"),
    ("render.stage", {"key": KEY, "layout": NO_LAYOUT}, "RenderStageParams"),
    ("render.stage", {"key": KEY, "layout": NO_LAYOUT, "stage": "render"}, "RenderStageParams"),
    ("render.stage", {"key": KEY, "layout": NO_LAYOUT, "stage": "octi", "width": 0},
     "RenderStageParams"),
    ("render.stage", {"key": KEY, "layout": NO_LAYOUT, "stage": "octi", "labels": "yes"},
     "RenderStageParams"),
    ("render.stage", {"key": KEY, "stage": "octi"}, "RenderStageParams"),
    ("graph.build", {"key": KEY, "mode": "Tram!"}, "GraphBuildParams"),
    ("graph.build", {"key": KEY, "label_pattern": ""}, "GraphBuildParams"),
    ("graph.build", {"key": KEY, "label_strip": 3}, "GraphBuildParams"),
    ("map.build", {"key": KEY, "date": DATE}, "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": "not-a-layout", "date": DATE}, "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "force": True},
     "MapBuildParams"),
    ("graph.build", None, "GraphBuildParams"),
    ("graph.build", [], "GraphBuildParams"),
    ("graph.build", {}, "GraphBuildParams"),
    ("graph.build", {"key": "../etc"}, "GraphBuildParams"),
    ("graph.build", {"key": "LA"}, "GraphBuildParams"),
    ("graph.build", {"key": KEY, "force": "yes"}, "GraphBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": "11/09/2026"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": "2026-13-40"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": 20260911}, "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "out": "../x"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "out": ".hidden"},
     "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "out": "a/b"}, "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "width": 0}, "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "width": "wide"},
     "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "colors": ["#123456"]},
     "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "colors": {"A": "red"}},
     "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "colors": {"A": "123456"}},
     "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "colors": {"A": None}},
     "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "default_color": "888888"},
     "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "default_color": "#8888"},
     "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "line_order": "A,B"},
     "MapBuildParams"),
    ("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "style": {}}, "MapBuildParams"),
    ("export.plan", None, "ExportPlanParams"),
    ("export.plan", {}, "ExportPlanParams"),
    ("export.plan", {"key": KEY}, "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": 3}, "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": "instagram-reel", "page": "not an address"},
     "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": "instagram-reel", "date": "tomorrow"},
     "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": "instagram-reel", "options": {"view": "plan"}},
     "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": "instagram-reel", "options": {"quality": "ultra"}},
     "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": "instagram-reel", "options": {"at": "7am"}},
     "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": "instagram-reel", "options": {"labels": "no"}},
     "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": "instagram-reel", "options": {"tag": "a/b"}},
     "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": "instagram-reel", "options": {"lines": "A,B"}},
     "ExportPlanParams"),
    ("export.plan", {"key": KEY, "preset": "instagram-reel", "mode": "video"},
     "ExportPlanParams"),
    ("export.encode", {}, "ExportEncodeParams"),
    ("export.encode", {"plan": {}, "source": "/a", "dest": "/b/x.mp4"}, "ExportEncodeParams"),
    ("export.encode", {"plan": "plan", "source": "/a", "dest": "/b/x.mp4"}, "ExportEncodeParams"),
]


def _plan_dict(**over):
    job = export.plan(KEY, "instagram-reel", **over)
    return {**job.to_dict(), "filename": job.filename}


# A plan handed back with one field wrong, per field the server checks.
BAD_PLANS = [
    {"mode": "gif"}, {"url": "no scheme"}, {"width": 0}, {"scale": 2.5}, {"fps": True},
    {"format": "webm"}, {"beats": "none"}, {"beats": [{"secs": 0}]},
    {"beats": [{"secs": 1, "view": "plan", "labels": None, "at": None, "speed": None,
                "sweep": False, "hours": None, "lo": None, "hi": None, "tween": None}]},
    {"beats": [{"secs": 1, "view": None, "labels": None, "at": "07:00", "speed": None,
                "sweep": False, "hours": None, "lo": None, "hi": None, "tween": None}]},
    {"keep": "yes"}, {"fade": -1}, {"stem": "../x"}, {"theme": "sepia"}, {"view": "plan"},
    {"storyboard": "unknown"}, {"at": "07:00"}, {"notes": "note"}, {"extra": 1},
]


@pytest.mark.parametrize("wrong", BAD_PLANS)
def test_a_plan_handed_back_is_checked_field_by_field(client, wrong):
    plan = {**_plan_dict(), **wrong}
    params = {"plan": plan, "source": "/somewhere/frames", "dest": "/elsewhere/out.mp4"}
    error = client.call("export.encode", params)["error"]
    assert error["code"] == -32602, error
    assert error["data"]["kind"] == "params"
    assert invalid(params, "ExportEncodeParams")


@pytest.mark.parametrize("params", [
    {"source": "frames", "dest": "/elsewhere/out.mp4"},
    {"source": "/somewhere/frames", "dest": "out.mp4"},
    {"source": "/somewhere/frames", "dest": str(config.REPO_ROOT / "out" / "x.mp4")},
    {"source": "/somewhere/frames", "dest": "/elsewhere/out.mp4", "provenance": {"trips": -1}},
    {"source": "/somewhere/frames", "dest": "/elsewhere/out.mp4", "provenance": {"x": 1}},
])
def test_encode_refuses_a_path_it_must_not_write_to(client, params):
    error = client.call("export.encode", {"plan": _plan_dict(), **params})["error"]
    assert error["code"] == -32602, error


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


def test_a_mode_loom_would_not_know_is_refused_by_hand(client):
    """The schema can only hold the shape; the names are the server's."""
    error = client.call("graph.build", {"key": KEY, "mode": "zeppelin"})["error"]
    assert error["code"] == -32602 and error["data"]["kind"] == "params"
    for mode in ("tram,subway", "rail,funicular", "all", "1"):
        assert feeds.valid_mode(mode)


def test_calendar_check_goes_past_the_pattern(client):
    # 2026-02-30 matches the pattern and is not a day.
    error = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT,
                                      "date": "2026-02-30"})["error"]
    assert error["code"] == -32602
    assert "not a calendar day" in error["data"]["hint"]


GOOD_PARAMS = [
    ("FeedsServiceParams", {"key": KEY}),
    ("FeedsServiceParams", {"key": KEY, "anchor": DATE}),
    ("FeedsServiceParams", {"key": KEY, "anchor": DATE, "lines": ["A", "E"]}),
    ("FeedsAddParams", {"source": "https://x.test/a.zip"}),
    ("FeedsAddParams", {"source": "/somewhere/a.zip", "key": "mine", "name": "Mine",
                        "mode": "tram", "agency": "M"}),
    ("FeedsRemoveParams", {"key": "mine"}),
    ("FeedsInspectParams", {"key": KEY}),
    ("FeedsInspectParams", {"key": KEY, "anchor": DATE}),
    ("RenderStageParams", {"key": KEY, "layout": NO_LAYOUT, "stage": "gtfs2graph"}),
    ("RenderStageParams", {"key": KEY, "layout": NO_LAYOUT, "stage": "octi", "width": 800,
                           "labels": True}),
    ("GraphBuildParams", {"key": KEY}),
    ("GraphBuildParams", {"key": KEY, "force": True}),
    ("GraphBuildParams", {"key": KEY, "agency": ""}),
    ("GraphBuildParams", {"key": KEY, "mode": "tram", "agency": "LACMTA",
                          "label_pattern": "^Metro (.+) Line$", "label_strip": "-N$"}),
    ("MapBuildParams", {"key": KEY, "layout": NO_LAYOUT, "date": DATE}),
    ("MapBuildParams", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "out": "p1",
                        "width": 900, "line_order": ["A", "B"]}),
    ("MapBuildParams", {"key": KEY, "layout": NO_LAYOUT, "date": DATE,
                        "colors": {"A": "#123456", "Z": "#ABCDEF"}, "default_color": "#00ff00"}),
    ("MapBuildParams", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "colors": {}}),
    ("ExportPlanParams", {"key": KEY, "preset": "instagram-reel"}),
    ("ExportPlanParams", {"key": KEY, "preset": "instagram-post",
                          "page": "app://local/projects/p1/la-metro-rail.html", "date": DATE,
                          "options": {"view": "map", "labels": True, "title": False,
                                      "clock": True, "theme": "light", "at": "07:30",
                                      "lines": ["A"], "storyboard": "tour",
                                      "quality": "draft", "fade": 0.5, "tag": "t",
                                      "safe": False}}),
    ("ExportEncodeParams", {"plan": _plan_dict(), "source": "/somewhere/frames",
                            "dest": "/elsewhere/out.mp4"}),
    ("ExportEncodeParams", {"plan": _plan_dict(), "source": "/somewhere/frames",
                            "dest": "/elsewhere/out.mp4",
                            "provenance": {"service_date": DATE, "trips": 100,
                                           "stations": 10, "lines": 2, "caveats": ["a"]}}),
]


@pytest.mark.parametrize("definition,params", GOOD_PARAMS)
def test_the_schema_accepts_what_the_server_accepts(definition, params):
    check(params, definition)


# -------------------------------------------------------------------- export

def test_export_presets_and_storyboards_are_the_tables(client):
    presets = client.call("export.presets")["result"]
    check(presets, "ExportPresets")
    reel = next(p for p in presets["presets"] if p["name"] == "instagram-reel")
    assert (reel["width"], reel["height"], reel["kind"], reel["format"]) == (1080, 1920, "video", "mp4")
    assert reel["storyboard"] == "tour" and reel["safe_zones"] is True
    assert {p["name"] for p in presets["presets"]} == set(export.PRESETS)

    boards = client.call("export.storyboards")["result"]
    check(boards, "ExportStoryboards")
    by_name = {b["name"]: b for b in boards["storyboards"]}
    assert set(by_name) == set(export.STORYBOARDS)
    assert by_name["tour"]["geographic"] is False
    assert by_name["transform"]["geographic"] is True
    assert by_name["transform"]["beats"][0]["at"] == "08:00"
    assert by_name["transform"]["views"] == "geographic -> map -> linear -> time"
    for name in ("export.presets", "export.storyboards"):
        assert client.call(name, {"x": 1})["error"]["code"] == -32602


def test_export_plan_is_the_module_plan_and_is_pure(client):
    result = client.call("export.plan", {"key": KEY, "preset": "instagram-reel"})["result"]
    check(result, "CaptureJob")
    assert result == _plan_dict()
    assert result["filename"] == "la-metro-rail-instagram-reel.mp4"
    assert result["mode"] == "video" and len(result["beats"]) == 4
    assert client.endpoint.jobs == {}, "a plan is not a job"


def test_export_plan_takes_the_apps_page_and_service_day(client):
    """The desktop app serves a project's page on its own origin and knows
    the project's service day; nothing in the answer is a path under the
    engine's own repository."""
    page = "app://local/projects/p1/la-metro-rail.html"
    result = client.call("export.plan", {
        "key": KEY, "preset": "instagram-post", "page": page, "date": "2026-09-11",
        "options": {"quality": "high", "at": "09:15", "theme": "light", "tag": "app"}})["result"]
    check(result, "CaptureJob")
    assert result["url"].startswith(page + "?present=1")
    assert "date=Friday+11+September+2026" in result["url"]
    assert "at=09%3A15" in result["url"]
    assert result["at"] == 9 * 3600 + 15 * 60
    assert result["filename"] == "la-metro-rail-instagram-post-light-app.png"
    assert str(config.REPO_ROOT) not in json.dumps(result)


def test_export_plan_refuses_with_the_export_modules_sentences(client):
    vector = client.call("export.plan", {"key": KEY, "preset": "portfolio-svg"})["error"]
    assert vector["code"] == -32000 and vector["data"]["kind"] == "export"
    assert "vector" in vector["data"]["hint"]
    # A geographic storyboard on a feed opted out of the geometry: the plan's
    # own refusal, with the sentence bin/export shows.
    plain = "opted-out"
    real = feeds.FEEDS[KEY]
    with mock.patch.dict(feeds.FEEDS, {**feeds.FEEDS, plain: replace(real, key=plain, geographic=False)},
                         clear=True):
        geo = client.call("export.plan", {"key": plain, "preset": "portfolio-mp4",
                                          "options": {"storyboard": "transform"}})["error"]
    assert geo["code"] == -32000 and geo["data"]["kind"] == "export"
    assert "geographic" in geo["data"]["hint"]


def _frames(into: Path, n: int, size: int, *, noise: bool = False) -> Path:
    """A square crossing the frame, or noise: near-identical frames encode
    in a blink, and a cancel test needs ffmpeg to still be at work."""
    from PIL import Image, ImageDraw
    into.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        if noise:
            im = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
        else:
            im = Image.new("RGB", (size, size), "#15120f")
            x = int(i / max(n - 1, 1) * (size - 12))
            ImageDraw.Draw(im).rectangle([x, size // 2 - 6, x + 12, size // 2 + 6],
                                         fill="#e8b04a")
        im.save(into / f"{i:06d}.png")
    return into


@needs_ffmpeg
def test_export_encode_reproduces_the_video_and_reports_progress(client, tmp_path):
    from PIL import Image, ImageChops
    frames = _frames(tmp_path / "frames", 12, 64)
    plan = client.call("export.plan", {"key": KEY, "preset": "linkedin-video",
                                       "options": {"quality": "high"}})["result"]
    outs = []
    for name in ("a", "b"):
        dest = tmp_path / name / plan["filename"]
        msg_id = client.send("export.encode", {
            "plan": plan, "source": str(frames), "dest": str(dest),
            "provenance": {"service_date": "2026-09-11", "trips": 321, "caveats": ["one"]}})
        result = client.wait(msg_id)["result"]
        check(result, "ExportEncodeResult")
        assert result["files"][0]["path"] == str(dest) and result["files"][0]["bytes"] > 0
        assert result["sidecar"]["file"] == plan["filename"]
        assert result["sidecar"]["service_date"] == "2026-09-11"
        assert result["sidecar"]["trips"] == 321
        assert result["sidecar"]["caveats"] == ["one"]
        assert export.sidecar_path(dest).exists()
        progress = client.notifications("job/progress", msg_id)
        assert progress and progress[-1]["fraction"] == 1.0
        assert all(p["stage"] == "encode" for p in progress)
        assert [p["fraction"] for p in progress] == sorted(p["fraction"] for p in progress)
        outs.append(dest)
    assert sorted(p.name for p in frames.iterdir()) == [f"{i:06d}.png" for i in range(12)]

    def decoded(video: Path, into: Path) -> list:
        into.mkdir()
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
                        str(into / "%06d.png")], check=True)
        return [Image.open(p).convert("RGB") for p in sorted(into.glob("*.png"))]

    da, db = decoded(outs[0], tmp_path / "da"), decoded(outs[1], tmp_path / "db")
    assert len(da) == len(db) == 12
    for x, y in zip(da, db):
        assert max(band[1] for band in ImageChops.difference(x, y).getextrema()) <= 8
    assert client.endpoint.jobs == {}


@needs_ffmpeg
def test_export_encode_refuses_a_source_that_is_not_there(client, tmp_path):
    plan = _plan_dict()
    error = client.call("export.encode", {"plan": plan, "source": str(tmp_path / "missing"),
                                          "dest": str(tmp_path / "out.mp4")})["error"]
    assert error["code"] == -32000 and error["data"]["kind"] == "io"
    assert not (tmp_path / "out.mp4").exists()


@needs_ffmpeg
def test_cancel_ends_ffmpeg_and_leaves_no_partial_file(client, tmp_path):
    """Enough frames at the preset's size that libx264 on its slow preset
    is still encoding when the cancel lands."""
    frames = _frames(tmp_path / "frames", 90, 400, noise=True)
    plan = client.call("export.plan", {"key": KEY, "preset": "linkedin-video"})["result"]
    dest = tmp_path / "out" / plan["filename"]
    msg_id = client.send("export.encode", {"plan": plan, "source": str(frames), "dest": str(dest)})
    wait_for(lambda: msg_id in client.endpoint.jobs
             and client.endpoint.jobs[msg_id].running is not None, 30, "ffmpeg to start")
    proc = client.endpoint.jobs[msg_id].running
    time.sleep(0.5)  # well inside the encode: noise at 1200x1200 on x264's slow preset
    assert proc.poll() is None, "ffmpeg was already done; the frames are too cheap"
    client.notify("$/cancelRequest", {"id": msg_id})

    response = client.wait(msg_id, timeout=30)
    assert response["error"]["code"] == -32800, response
    assert proc.poll() is not None, "ffmpeg is still running"
    assert client.endpoint.jobs == {}
    assert not dest.exists(), "a partial file was left behind"
    assert not export.sidecar_path(dest).exists()
    assert sorted(p.name for p in frames.iterdir()) == [f"{i:06d}.png" for i in range(90)]


# -------------------------------------------------------------------- errors

def test_errors_are_classified_by_the_module_that_wrote_the_sentence():
    tables = {"trips": pd.DataFrame({"service_id": [], "route_id": []})}
    try:
        schedule.busiest_weekday(tables, anchor=dt.date(2026, 9, 10))
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


def test_a_cancel_that_arrives_while_a_request_waits_on_no_process_is_honoured(client):
    """feeds.service starts no process, so a cancel cannot interrupt it: the
    work runs to its end, and the request is answered with the cancelled
    error and never with a result the client has stopped waiting for."""
    endpoint = client.endpoint
    endpoint._request_id = 41

    def work(job, _progress):
        client.notify("$/cancelRequest", {"id": 41})
        assert job.cancelled
        return {"answered": "anyway"}

    run = endpoint._job(work)
    endpoint._request_id = None
    assert 41 in endpoint.jobs
    with pytest.raises(JsonRpcRequestCancelled):
        run()
    assert endpoint.jobs == {}


def test_cancel_for_an_unknown_request_is_ignored(client, caplog):
    client.notify("$/cancelRequest", {"id": 999})
    assert client.out == []


# ----------------------------------------------------------- the service day

needs_feed = pytest.mark.skipif(not all(z.exists() for z in SOURCE_ZIPS),
                                reason="needs the cached LA feed")
needs_cdmx = pytest.mark.skipif(not all(z.exists() for z in CDMX_ZIPS),
                                reason="needs the cached Mexico City feed")


@needs_feed
def test_feeds_service_answers_the_window_and_the_day_from_the_anchor(client, home):
    """What the library says, over the protocol: the feed's window, and the
    busiest weekday scanning from the anchor, which is echoed so the caller
    can store it. No LOOM: the calendar is read, nothing is laid out."""
    tables = feeds.tables(KEY)
    start, end = schedule.service_window(tables)
    anchor = dt.date(2026, 9, 10)
    result = client.call("feeds.service", {"key": KEY, "anchor": anchor.isoformat()},
                         timeout=120)["result"]
    check(result, "FeedsServiceResult")
    day = schedule.busiest_weekday(tables, anchor=anchor)
    assert result == {"start": start.isoformat(), "end": end.isoformat(),
                      "busiest_weekday": day.isoformat(), "anchor": "2026-09-10"}
    assert client.endpoint.jobs == {}

    # On the map's lines the trips are counted as the map build counts them.
    lines = ["A", "E"]
    on_lines = client.call("feeds.service", {"key": KEY, "anchor": anchor.isoformat(),
                                             "lines": lines}, timeout=120)["result"]
    check(on_lines, "FeedsServiceResult")
    assert on_lines["busiest_weekday"] == schedule.busiest_weekday(
        tables, set(lines), anchor=anchor).isoformat()
    assert on_lines["anchor"] == "2026-09-10"

    # Without an anchor the engine's today is used, and echoed: read before
    # and after the call, since the call may straddle midnight.
    before = dt.date.today()
    today = client.call("feeds.service", {"key": KEY}, timeout=120)["result"]
    after = dt.date.today()
    check(today, "FeedsServiceResult")
    assert today["anchor"] in {before.isoformat(), after.isoformat()}
    assert today["busiest_weekday"] == schedule.busiest_weekday(
        tables, anchor=dt.date.fromisoformat(today["anchor"])).isoformat()


@needs_cdmx
def test_feeds_service_on_an_expired_feed_still_answers_a_day_inside_its_window(client, home):
    tables = feeds.tables(CDMX)
    start, end = schedule.service_window(tables)
    result = client.call("feeds.service", {"key": CDMX, "anchor": "2026-09-10"},
                         timeout=120)["result"]
    check(result, "FeedsServiceResult")
    assert (result["start"], result["end"]) == (start.isoformat(), end.isoformat())
    assert start <= dt.date.fromisoformat(result["busiest_weekday"]) <= end
    assert result["busiest_weekday"] == schedule.busiest_weekday(
        tables, anchor=dt.date(2026, 9, 10)).isoformat()


def test_feeds_service_refuses_an_unknown_feed_and_a_bad_anchor(client):
    error = client.call("feeds.service", {"key": "not-a-feed"})["error"]
    assert error["code"] == -32000 and error["data"]["kind"] == "feed"
    error = client.call("feeds.service", {"key": KEY, "anchor": "2026-02-30"})["error"]
    assert error["code"] == -32602 and "not a calendar day" in error["data"]["hint"]
    assert error["data"]["hint"].startswith("anchor: ")
    # A null anchor is refused as an anchor, not with map.build's sentence
    # about a date the engine never picks: this method picks one.
    error = client.call("feeds.service", {"key": KEY, "anchor": None})["error"]
    assert error["code"] == -32602
    assert error["data"]["hint"].startswith("anchor must be a calendar day")
    assert "left out" in error["data"]["hint"]


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
    # Stored under its id, with the meta beside it; the id is what the app keeps.
    assert re.fullmatch(r"[0-9a-f]{64}", result["layout"])
    layout_dir = home / "data" / "graphs" / KEY / result["layout"]
    for stage, path in result["paths"].items():
        assert Path(path).is_file()
        assert Path(path).parent == layout_dir
    assert (layout_dir / ".meta.json").is_file()
    assert result["meta"]["feed"] == KEY and result["meta"]["migrated"] is False
    assert pipeline.stored(KEY).id == result["layout"]
    assert client.endpoint.jobs == {}

    # Asked again, the stored layout is answered without a stage running.
    again = client.call("graph.build", {"key": KEY}, timeout=60)["result"]
    assert again["layout"] == result["layout"]


@needs_native
def test_graph_build_over_the_native_backend(client, home):
    """The same four stages from the binaries the app ships, into the same
    cache, with the feed unpacked beside its zip; a cancel ends the binary."""
    assert client.call("engine.info")["result"]["loom"]["backend"] == "native"
    msg_id = client.send("graph.build", {"key": KEY, "force": True})
    response = client.wait(msg_id, timeout=180)
    assert "result" in response, response
    check(response["result"], "GraphBuildResult")
    progress = client.notifications("job/progress", msg_id)
    assert [p["stage"] for p in progress] == ["gtfs2graph", "topo", "loom", "octi"]
    assert response["result"]["stages"]["octi"]["stations"] > 50
    assert (home / "data" / "feeds" / f"{KEY}.normalized").is_dir()
    for path in response["result"]["paths"].values():
        assert Path(path).is_file()

    second = client.send("graph.build", {"key": KEY, "force": True})
    wait_for(lambda: second in client.endpoint.jobs
             and client.endpoint.jobs[second].running is not None, 30, "gtfs2graph to start")
    proc = client.endpoint.jobs[second].running
    time.sleep(0.5)
    client.notify("$/cancelRequest", {"id": second})
    response = client.wait(second, timeout=30)
    assert response["error"]["code"] == -32800, response
    assert proc.poll() is not None, "the binary is still running"
    assert client.endpoint.jobs == {}


@needs_loom
def test_cancel_ends_the_loom_process_and_leaves_the_cache_alone(client, home):
    stored = pipeline.stored(KEY)
    cached = None if stored is None else stored.paths["gtfs2graph"]
    before = cached.stat().st_mtime_ns if cached is not None else None

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
        assert cached.stat().st_mtime_ns == before, "a cancelled stage overwrote the layout"
    assert not list((home / "data" / "graphs" / KEY).glob("*.building")), "a scratch was left"


@needs_loom
def test_a_mode_that_matches_nothing_returns_the_hint(client, home):
    """An override on the request: LA has no ferries, so the layout it names
    is empty and refused by name, and the registry's layout is untouched."""
    error = client.call("graph.build", {"key": KEY, "mode": "ferry"}, timeout=180)["error"]
    assert error["code"] == -32000
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "feed"
    assert "matched no routes" in error["data"]["hint"]
    assert "'ferry'" in error["data"]["hint"]


def stored_layout(client) -> str:
    """The LA layout's id, from graph.build: instant once it is stored."""
    return client.call("graph.build", {"key": KEY}, timeout=180)["result"]["layout"]


@needs_loom
def test_map_build_writes_under_out_and_reports_diagnostics(client, home):
    layout = stored_layout(client)
    msg_id = client.send("map.build", {"key": KEY, "layout": layout, "date": DATE, "out": "p1"})
    response = client.wait(msg_id)
    assert "result" in response, response
    result = response["result"]
    check(result, "MapBuildResult")

    assert result["layout"] == layout
    assert result["date"] == DATE
    assert set(result["files"]) == {"svg", "html", "positions", "thumb_dark", "thumb_light"}
    for kind, path in result["files"].items():
        assert Path(path).is_file(), kind
        assert Path(path).parent == home / "out" / "p1"
    for kind in ("thumb_dark", "thumb_light"):
        thumb = Path(result["files"][kind]).read_text(encoding="utf-8")
        assert "var(" not in thumb and "<text" not in thumb and "<rect" not in thumb, kind
        assert len(thumb.encode()) < 100_000, kind
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


@needs_loom
def test_map_build_takes_a_colour_per_line_and_a_default(client, home):
    """The two colour inputs reach the map's strokes and the page's data
    through the server as they do through the library (test_colors.py)."""
    layout = stored_layout(client)
    result = client.call("map.build", {"key": KEY, "layout": layout, "date": DATE, "out": "p3",
                                       "colors": {"A": "#123456", "ZZ": "#000000"},
                                       "default_color": "#abcdef"},
                         timeout=180)["result"]
    svg = Path(result["files"]["svg"]).read_text()
    assert '<g class="line" data-line="A" stroke="#123456"' in svg
    assert '<g class="line" data-line="B" stroke="#eb131b"' in svg
    lines = json.loads(Path(result["files"]["positions"]).read_text())["lines"]
    assert lines["A"] == "#123456" and lines["B"] == "#eb131b" and "ZZ" not in lines
    # The thumbnails carry the same colours, as literals, in both palettes.
    for kind in ("thumb_dark", "thumb_light"):
        thumb = Path(result["files"][kind]).read_text(encoding="utf-8")
        drawn = re.findall(r'<g stroke="(#[0-9a-f]{6})"', thumb)
        assert "#123456" in drawn and "#eb131b" in drawn and "#0072bc" not in drawn, kind
    assert set(lines) == set(result["diagnostics"]["lines"])


@needs_loom
def test_map_build_never_lays_out(client, home, monkeypatch):
    """With a stored layout the map is drawn without a LOOM process; asked
    for a layout that is not stored it refuses rather than laying out."""
    layout = stored_layout(client)

    def no_loom(*args, **kwargs):
        raise AssertionError("map.build started a LOOM process")

    monkeypatch.setattr(loom, "execute", no_loom)
    result = client.call("map.build", {"key": KEY, "layout": layout, "date": DATE, "out": "p2"},
                         timeout=180)["result"]
    assert result["layout"] == layout
    error = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE},
                        timeout=30)["error"]
    assert error["code"] == -32000 and error["data"]["kind"] == "layout"
    assert "lay the feed out first" in error["data"]["hint"]


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


# ------------------------------------------------------------- the registry

from test_feeds import GOOD, gtfs_zip  # the fixture zip the feed tests build


def test_feeds_list_is_every_preset_and_what_was_added(client, home, tmp_path):
    before = client.call("feeds.list")["result"]
    check(before, "FeedsList")
    keys = [f["key"] for f in before["feeds"]]
    assert keys == list(feeds.FEEDS)
    la = next(f for f in before["feeds"] if f["key"] == KEY)
    assert la["source"] == "preset" and la["cached"] is True
    assert la["label_pattern"] == feeds.FEEDS[KEY].label_pattern

    src = gtfs_zip(tmp_path / "Metro de Prueba.zip")
    msg_id = client.send("feeds.add", {"source": str(src)})
    added = client.wait(msg_id)["result"]
    check(added, "FeedRecord")
    assert added["key"] == "metro-de-prueba" and added["source"] == "user"
    assert added["cached"] is True and added["url"] == ""
    # A file needs no download; the check reported once.
    stages = [p["stage"] for p in client.notifications("job/progress", msg_id)]
    assert stages == ["check"]

    after = client.call("feeds.list")["result"]
    assert [f["key"] for f in after["feeds"]] == keys + ["metro-de-prueba"]

    # A new endpoint, as a new process would be, sees it too.
    other = Client()
    try:
        again = other.call("feeds.list")["result"]
        assert "metro-de-prueba" in [f["key"] for f in again["feeds"]]
        inspected = other.call("feeds.inspect", {"key": "metro-de-prueba",
                                                 "anchor": "2026-09-10"})["result"]
        check(inspected, "Inspection")
        assert [r["label"] for r in inspected["routes"]] == ["1"]
    finally:
        other.endpoint.close()

    removed = client.call("feeds.remove", {"key": "metro-de-prueba"})["result"]
    check(removed, "Ok")
    assert "metro-de-prueba" not in feeds.all()
    error = client.call("feeds.remove", {"key": KEY})["error"]
    assert error["code"] == -32000 and error["data"]["kind"] == "feed"
    assert "built-in" in error["data"]["hint"]


def test_feeds_list_and_add_answer_headways_and_the_schema_requires_it(client, tmp_path,
                                                                       monkeypatch):
    """Issue 46: every feed answers whether it publishes headways, a preset by
    the registry's word and a feed added from a zip by what its check found.
    A home of its own: it needs no feed on disk and leaves the shared one as
    it was."""
    monkeypatch.setenv(config.ENV, str(tmp_path / "home"))
    listed = client.call("feeds.list")["result"]
    check(listed, "FeedsList")
    assert {f["key"]: f["headways"] for f in listed["feeds"]} == {
        key: key == "cdmx-metro" for key in feeds.FEEDS}

    # The field is required and a boolean: a record without it, or with a word
    # for it, is not a FeedRecord.
    bart = next(f for f in listed["feeds"] if f["key"] == "bart")
    assert not invalid(bart, "FeedRecord")
    assert invalid({k: v for k, v in bart.items() if k != "headways"}, "FeedRecord")
    assert invalid({**bart, "headways": "yes"}, "FeedRecord")

    frequencies = ("trip_id,start_time,end_time,headway_secs\n"
                   "T1,05:00:00,23:00:00,300\n")
    runs = gtfs_zip(tmp_path / "runs.zip", {**GOOD, "frequencies.txt": frequencies})
    plain = gtfs_zip(tmp_path / "plain.zip")
    answers = {}
    for key, src in (("runs-by-interval", runs), ("runs-by-timetable", plain)):
        sent = client.send("feeds.add", {"source": str(src), "key": key})
        answers[key] = client.wait(sent)["result"]
        check(answers[key], "FeedRecord")
    assert answers["runs-by-interval"]["headways"] is True
    assert answers["runs-by-timetable"]["headways"] is False

    after = client.call("feeds.list")["result"]
    check(after, "FeedsList")
    assert {f["key"]: f["headways"] for f in after["feeds"] if f["source"] == "user"} == {
        "runs-by-interval": True, "runs-by-timetable": False}


def test_feeds_add_refuses_a_zip_without_a_timetable_with_the_named_sentence(client, home,
                                                                              tmp_path):
    tables = {k: v for k, v in GOOD.items() if k != "stop_times.txt"}
    src = gtfs_zip(tmp_path / "partial.zip", tables)
    error = client.call("feeds.add", {"source": str(src)})["error"]
    assert error["code"] == -32000, error
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "feed"
    assert error["data"]["hint"] == ("partial.zip has no stop_times.txt, so there is no "
                                     "timetable to animate")
    # Nothing kept: no record, and no zip beyond the presets' (the module's
    # home is shared, so the record file itself may exist, empty).
    assert not any(f.source == "user" for f in feeds.all().values())
    assert not list((home / "data" / "feeds").glob("partial*"))


def test_feeds_add_by_url_reports_the_bytes_and_a_cancel_leaves_nothing(client, home, tmp_path,
                                                                         monkeypatch):
    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()

    class Response:
        content = payload

        def __init__(self):
            self.headers = {"Content-Length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for i in range(0, len(self.content), 32):
                time.sleep(0.005)
                yield self.content[i:i + 32]

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    msg_id = client.send("feeds.add", {"source": "https://example.test/gtfs.zip",
                                       "key": "remote", "name": "Remote"})
    result = client.wait(msg_id)["result"]
    check(result, "FeedRecord")
    assert result["url"] == "https://example.test/gtfs.zip"
    reports = client.notifications("job/progress", msg_id)
    downloads = [p for p in reports if p["stage"] == "download"]
    assert downloads and downloads[-1]["fraction"] == 1.0
    # The sentence feeds.add has always sent, word for word: E36 moved its
    # making into one function shared with a preset's download.
    assert re.fullmatch(r"downloaded [\d,]+ of [\d,]+ bytes", downloads[-1]["message"])
    assert reports[-1] == {"id": msg_id, "stage": "check", "fraction": 1.0,
                           "message": "checked the feed's tables"}

    # Cancelled during the download: the cancelled error, and nothing kept.
    msg_id = client.send("feeds.add", {"source": "https://example.test/gtfs.zip",
                                       "key": "stopped"})
    wait_for(lambda: client.notifications("job/progress", msg_id), 10, "the first chunk")
    client.notify("$/cancelRequest", {"id": msg_id})
    error = client.wait(msg_id)["error"]
    assert error["code"] == JsonRpcRequestCancelled.CODE
    assert "stopped" not in feeds.all()
    assert client.endpoint.jobs == {}


def test_a_failed_add_from_a_keyed_address_tells_nobody_the_key(client, home, monkeypatch,
                                                               caplog):
    """Issue 32. A feed can be added from a keyed link, and what a failed add
    says goes to three places a person may share: the error a client shows,
    its detail, and the traceback this process logs. None of them names the
    key, the user or the fragment, and ``requests``' own sentence - which
    repeats the address - is in none of them."""
    keyed = "https://someone:pw@example.test/feeds/gtfs.zip?api_key=S3CRET#tok"

    class Answer:
        status_code = 403
        reason = "Forbidden"

    def get(url, **kw):
        assert url == keyed, "the fetch itself asks for the whole address"
        raise feeds.requests.HTTPError(f"403 Client Error: Forbidden for url: {url}",
                                       response=Answer())

    monkeypatch.setattr(feeds.requests, "get", get)
    # The module's home is shared, so other tests' feeds may be in it.
    before = sorted(feeds.all())
    # The level the engine logs at unless SCHEMATIC_LOG says otherwise. At
    # debug the protocol library prints every request as it arrived, which is
    # the address as the client gave it: a person who asks for the wire gets
    # the wire.
    with caplog.at_level("INFO"):
        error = client.wait(client.send("feeds.add", {"source": keyed}))["error"]
    assert error["code"] == -32000, error
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "feed"
    assert error["data"]["hint"] == (
        "https://<redacted>@example.test/feeds/gtfs.zip?api_key=<redacted>#<redacted> "
        "could not be fetched: the server answered 403 Forbidden")
    logged = "\n".join(
        [r.getMessage() for r in caplog.records]
        + [logging.Formatter().formatException(r.exc_info)
           for r in caplog.records if r.exc_info])
    assert "could not be fetched" in logged, "the failure was logged, with its traceback"
    assert keyed not in json.dumps(client.out)
    for text in (json.dumps(error), logged, json.dumps(client.out)):
        for secret in ("S3CRET", "someone", "pw@", "tok", "Client Error"):
            assert secret not in text, f"{secret!r} is in {text!r}"
    assert sorted(feeds.all()) == before, "nothing was kept"


def test_feeds_inspect_for_la_is_the_librarys_answer(client, home):
    result = client.call("feeds.inspect", {"key": KEY, "anchor": "2026-09-10"})["result"]
    check(result, "Inspection")
    assert len(result["routes"]) == 6
    assert [t["route_type"] for t in result["route_types"]] == [0, 1]
    assert result["suggested_mode"] == "all"
    assert result == feeds.inspect(KEY, anchor=dt.date(2026, 9, 10)).to_dict()


LA_GRAPHS = config.REPO_ROOT / "data" / "graphs" / KEY
needs_la_layout = pytest.mark.skipif(
    not any(LA_GRAPHS.glob("*/03_octi.json")) if LA_GRAPHS.is_dir() else True,
    reason="needs a stored Los Angeles layout in the checkout")


@needs_la_layout
def test_render_stage_draws_a_stored_stage_with_graph_builds_counts(client, home):
    """The stage graphs the checkout stores for LA, copied under the home;
    graph.build answers the set without running, and render.stage draws
    from the same set, so the counts agree."""
    shutil.copytree(LA_GRAPHS, home / "data" / "graphs" / KEY, dirs_exist_ok=True)
    built = client.call("graph.build", {"key": KEY}, timeout=120)["result"]
    for stage in ("gtfs2graph", "loom"):
        result = client.call("render.stage", {"key": KEY, "layout": built["layout"],
                                              "stage": stage, "width": 800},
                             timeout=120)["result"]
        check(result, "RenderStageResult")
        assert result["svg"].lstrip().startswith("<svg")
        assert result["counts"] == built["stages"][stage]
        assert result["width"] > 0 and result["height"] > 0
    missing = client.call("render.stage", {"key": KEY, "layout": NO_LAYOUT,
                                           "stage": "octi"})["error"]
    assert missing["code"] == -32000 and missing["data"]["kind"] == "layout"
    assert "lay the feed out first" in missing["data"]["hint"]


@needs_loom
def test_a_feed_added_through_the_protocol_builds(client, home):
    """The acceptance criterion: a feed added by the protocol survives a new
    endpoint and lays out. LA's cached zip stands in for a download."""
    src = home / "data" / "feeds" / f"{KEY}.zip"
    added = client.call("feeds.add", {"source": str(src), "key": "la-user",
                                      "name": "LA as a user feed"}, timeout=120)["result"]
    assert added["source"] == "user"
    other = Client()
    try:
        built = other.call("graph.build", {"key": "la-user"}, timeout=300)["result"]
        check(built, "GraphBuildResult")
        assert built["stages"]["octi"]["stations"] > 50
    finally:
        other.endpoint.close()
    client.call("feeds.remove", {"key": "la-user"})



def test_a_long_request_reports_a_presets_download_and_a_cancel_keeps_nothing(client, tmp_path,
                                                                                monkeypatch):
    """E36, over the protocol: a feed downloaded inside any long request - here
    the work fetches a preset, as a first layout or inspection does - reports
    stage download with its bytes, and a cancel during it answers the
    cancelled error and caches nothing."""
    import zipfile

    monkeypatch.setenv(config.ENV, str(tmp_path))
    source = tmp_path / "remote.zip"
    with zipfile.ZipFile(source, "w") as z:
        z.writestr("agency.txt", "agency_id,agency_name\nA,A\n")
    payload = source.read_bytes()
    monkeypatch.setitem(feeds.FEEDS, "t", feeds.Feed(key="t", name="T",
                                                     url="https://example.test/t.zip"))

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for i in range(0, len(payload), 32):
                yield payload[i:i + 32]

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    endpoint = client.endpoint

    endpoint._request_id = 51
    run = endpoint._job(lambda _job, _progress: {"zip": feeds.fetch("t").name})
    endpoint._request_id = None
    assert run() == {"zip": "t.zip"}
    progress = client.notifications("job/progress", 51)
    assert progress and all(p["stage"] == "download" for p in progress)
    assert progress[-1]["fraction"] == 1.0
    assert progress[-1]["message"] == f"downloaded {len(payload):,} of {len(payload):,} bytes"

    feeds.get("t").zip_path.unlink()
    endpoint._request_id = 52

    def cancelled_midway(job, _progress):
        original = feeds.requests.get

        def get(url, **kw):
            response = original(url, **kw)
            chunks = response.iter_content

            def iter_content(size):
                for i, chunk in enumerate(chunks(size)):
                    if i == 1:
                        client.notify("$/cancelRequest", {"id": 52})
                    yield chunk
            response.iter_content = iter_content
            return response

        monkeypatch.setattr(feeds.requests, "get", get)
        return feeds.fetch("t")

    run = endpoint._job(cancelled_midway)
    endpoint._request_id = None
    with pytest.raises(JsonRpcRequestCancelled):
        run()
    assert sorted(p.name for p in config.feeds_dir().iterdir()) == []
