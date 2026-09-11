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
import os
import re
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
from unittest import mock
from jsonschema import Draft202012Validator

from pylsp_jsonrpc.exceptions import JsonRpcRequestCancelled

from schematic import __version__, config, export, feeds, loom, pipeline, schedule, serve

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
    assert client.call("feeds.list")["error"]["code"] == -32601


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


BAD_PARAMS = [
    ("feeds.service", None, "FeedsServiceParams"),
    ("feeds.service", {}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "anchor": "tomorrow"}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "anchor": 20260910}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "anchor": "2026-02-30"}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "anchor": None}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "lines": "A"}, "FeedsServiceParams"),
    ("feeds.service", {"key": KEY, "date": DATE}, "FeedsServiceParams"),
    ("graph.build", {"key": KEY, "mode": "Tram!"}, "GraphBuildParams"),
    ("graph.build", {"key": KEY, "agency": ""}, "GraphBuildParams"),
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
    ("GraphBuildParams", {"key": KEY}),
    ("GraphBuildParams", {"key": KEY, "force": True}),
    ("GraphBuildParams", {"key": KEY, "mode": "tram", "agency": "LACMTA",
                          "label_pattern": "^Metro (.+) Line$", "label_strip": "-N$"}),
    ("MapBuildParams", {"key": KEY, "layout": NO_LAYOUT, "date": DATE}),
    ("MapBuildParams", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "out": "p1",
                        "width": 900, "line_order": ["A", "B"]}),
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
