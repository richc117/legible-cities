"""Every method's params, judged by the schema and by the server alike
(engine issue 83).

The server checks its params by hand; the desktop app's generated types
and its stand-in engine follow the schema. test_serve's ``BAD_PARAMS`` and
``GOOD_PARAMS`` are picked by hand, and the second was only ever shown to
the schema. Here every method starts from params both accept, and each
field in turn, at every depth, is left out, given a value of every JSON
type, taken to each of its bounds and just past it, given each of its
enum's names and one more, and has a key no definition names put beside
it; the first and the last item of a list are changed the same way. Each
case goes through ``jsonschema`` against the method's params and through
the live server, whose long methods are stopped before any work starts.
The two must agree, and the server must answer every case as it means to:
the params error, a result, or an engine error of a kind it names.

Where the server refuses what the schema lets through, it is one of the
rules the schema cannot hold, each written in a definition's description
and named in ``SERVER_ONLY``, and nothing else; each is written here from
that description, never by asking the engine's own check, which would
excuse whatever that check refuses. The server never accepts what the
schema refuses.

One rule stands over the schema. A number no double holds (NaN, the
infinities, an integer past about 1.8e308) is not one the desktop app can
send, and NaN is not even JSON, though Python's json reads it; jsonschema
compares such a number as Python does, where NaN passes every bound. The
server refuses it with the params error wherever it stands, as the
schema's top-level description says, and the test expects that whatever
jsonschema answers (``doubles_only``).
"""

from __future__ import annotations

import copy
import math
import os
import sys
import tempfile
from typing import Any, Callable, Iterator

import pytest
from jsonschema import Draft202012Validator

from test_serve import DATE, KEY, NO_LAYOUT, Client, _plan_dict

from schematic import export, feeds, serve

SCHEMA = serve.schema()
DEFS = SCHEMA["$defs"]
MISSING = object()

# Absolute on every platform, outside the repository, and a folder that is
# there, as a video's frames must be.
FRAMES = tempfile.gettempdir()
DEST = os.path.join(tempfile.gettempdir(), "elsewhere", "out.mp4")

BEATS = [
    {"secs": 4, "view": "map", "labels": True, "at": "06:30", "speed": 60, "sweep": False,
     "hours": None, "span": None, "tween": 0, "card": False, "draw_in": False},
    {"secs": 4, "view": "map", "labels": None, "at": None, "speed": None, "sweep": False,
     "hours": 1, "span": ["07:00", "08:00"], "tween": 1, "card": False, "draw_in": False},
]

# Params both accept, per method: every field the definition has, where
# one base can hold them all.
BASES: dict[str, list[Any]] = {
    "engine.info": [None, {}],
    "engine.shutdown": [None],
    "style.presets": [None],
    "export.presets": [None],
    "export.storyboards": [None],
    "feeds.list": [None],
    "feeds.service": [{"key": KEY, "anchor": DATE, "lines": ["A", "B"]}],
    "feeds.add": [{"source": "https://x.test/a.zip", "key": "mine", "name": "Mine",
                   "mode": "tram", "agency": "M"}],
    "feeds.remove": [{"key": "mine"}],
    "feeds.inspect": [{"key": KEY, "anchor": DATE}],
    "render.stage": [{"key": KEY, "layout": NO_LAYOUT, "stage": "octi", "width": 800,
                      "labels": True, "date": DATE}],
    "graph.build": [{"key": KEY, "mode": "tram", "agency": "LACMTA",
                     "label_pattern": "^Metro (.+) Line$", "label_strip": "-N$", "force": False,
                     "tuning": {"merge_distance": 50, "grid": "octilinear", "grid_size": 100,
                                "penalties": {"deg45": 2, "deg90": 1.5, "deg135": 1,
                                              "deg180": 0, "diagonal": 0.5}}}],
    "map.build": [
        {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "out": "p1", "width": 900,
         "colors": {"A": "#123456"}, "default_color": "#00ff00", "line_order": ["A", "B"],
         "style": {"line_width": 7, "line_gap": 1.6, "station_radius": 1,
                   "interchange_radius": 30, "station_stroke": 2.2, "label_size": 11,
                   "label_offset": 9, "padding": 24, "background": "#000000",
                   "station_fill": "#ffffff", "station_stroke_color": "#111111",
                   "label_color": "#abcdef", "station_shape": "circle",
                   "interchange_shape": "circle", "label_font": "system"},
         "dot_radius": 5, "trail": 1,
         "lines": {"A": {"name": "Airport", "hidden": False, "width": 1,
                         "casing": {"width": 0.5, "color": "#ffffff"}, "dash": "solid"}}},
        {"key": KEY, "layout": NO_LAYOUT, "date": DATE,
         "style": {"preset": "beck", "label_font": "inter"}},
        {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "style": {}},
    ],
    "export.plan": [
        {"key": KEY, "preset": "instagram-reel", "page": "https://example.test/p.html",
         "date": DATE, "out": "p1",
         "options": {"view": "map", "labels": True, "title": True, "clock": True,
                     "theme": "dark", "at": "07:30", "lines": ["A", "B"], "storyboard": "tour",
                     "quality": "draft", "fade": 0.5, "tag": "t", "safe": False,
                     "caption": "Rush hour", "clock_corner": "top-right"}},
        {"key": KEY, "preset": "instagram-reel", "options": {"storyboard": BEATS}},
    ],
    "export.encode": [
        {"plan": _plan_dict(), "source": FRAMES, "dest": DEST,
         "provenance": {"service_date": DATE, "trips": 100, "stations": 10, "lines": 2,
                        "caveats": ["a", "b"], "alt": "A map of the network."}},
        {"plan": {**_plan_dict(), "colors": ["#123456", "#abcdef"]}, "source": FRAMES,
         "dest": DEST},
    ],
}

# Values every field is given, whatever its type: one of each JSON type,
# a whole number written as a decimal, a negative, an empty string, a
# string of spaces, one that is no regular expression, and empty and
# filled arrays and objects.
PALETTE = (None, True, 0, 7, 7.0, 7.5, -1, 10 ** 400, float("nan"), float("inf"),
           float("-inf"), "", "zzz", "   ", "(", [], ["zzz"], {}, {"zzz": 1})


def resolve(node: Any) -> dict:
    """A schema node with its ``$ref`` followed; an empty one for none."""
    while isinstance(node, dict) and "$ref" in node:
        node = DEFS[node["$ref"].rsplit("/", 1)[1]]
    return node if isinstance(node, dict) else {}


def shaped(node: Any, value: Any) -> dict:
    """The branch of a ``oneOf`` that has ``value``'s shape, or the node."""
    node = resolve(node)
    want = "array" if isinstance(value, list) else "object" if isinstance(value, dict) else None
    for branch in node.get("oneOf", []):
        if want is not None and resolve(branch).get("type") == want:
            return resolve(branch)
    return node


def child(node: Any, value: Any, key: Any) -> Any:
    """The schema of ``value[key]``, as far as one can be found."""
    node = shaped(node, value)
    if isinstance(key, int):
        prefix = node.get("prefixItems", [])
        return prefix[key] if key < len(prefix) else node.get("items")
    if key in node.get("properties", {}):
        return node["properties"][key]
    extra = node.get("additionalProperties")
    return extra if isinstance(extra, dict) else None


def names(node: Any) -> list[Any]:
    """The values a node names outright: its enum, its const, its branches'."""
    node = resolve(node)
    out = list(node.get("enum", []))
    if "const" in node:
        out.append(node["const"])
    for branch in node.get("oneOf", []):
        out += names(branch)
    return out


def values_for(node: Any, current: Any) -> list[Any]:
    """What a field is given: the palette, its bounds and either side of
    each, lengths at and past its bounds, its names and one more, and its
    current value reshaped."""
    out: list[Any] = list(PALETTE)
    node = resolve(node)
    for bound in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if bound in node:
            b = node[bound]
            out += [b - 1, b - 0.5, b, float(b), b + 0.5, b + 1]
    for length in {node.get("minLength", 0), node.get("maxLength", 0)}:
        for n in (length - 1, length, length + 1):
            if n >= 0:
                out += ["a" * n, " " + "a" * n]
    named = names(node)
    out += named + ([named[0] + "x"] if named and isinstance(named[0], str) else [])
    if isinstance(current, bool):
        out.append(not current)
    elif isinstance(current, int):
        out.append(float(current))
    elif isinstance(current, str):
        out += [" " + current + " ", current.upper(), current + "\u2028"]
    elif isinstance(current, list) and current:
        out += [current[:1], current + [current[-1]], (current * 17)[:17]]
    return out


def cases(value: Any, node: Any, path: tuple = ()) -> Iterator[tuple[tuple, Any]]:
    """Each change to one place in ``value``, as (the place, what goes
    there), ``MISSING`` for taking it out; then the same inside it."""
    if isinstance(value, dict):
        shape = shaped(node, value)
        for name in sorted(set(value) | set(shape.get("properties", {}))):
            below = child(node, value, name)
            if name in value:
                yield path + (name,), MISSING
            for candidate in values_for(below, value.get(name)):
                yield path + (name,), candidate
            if name in value:
                yield from cases(value[name], below, path + (name,))
        yield path + ("zzz",), 1
        if value and isinstance(shape.get("additionalProperties"), dict):
            yield path + ("zzz",), copy.deepcopy(next(iter(value.values())))
    elif isinstance(value, list) and value:
        # The first item and the last: a list of beats holds its first to
        # rules of its own.
        for at in sorted({0, len(value) - 1}):
            below = child(node, value, at)
            yield path + (at,), MISSING
            for candidate in values_for(below, value[at]):
                yield path + (at,), candidate
            yield from cases(value[at], below, path + (at,))


def changed(base: Any, path: tuple, value: Any) -> Any:
    out = copy.deepcopy(base)
    holder = out
    for key in path[:-1]:
        holder = holder[key]
    if value is MISSING:
        del holder[path[-1]]
    else:
        holder[path[-1]] = value
    return out


# ------------------------------------------------- the rules the schema cannot hold

def _style(params: dict) -> dict:
    return params.get("style") if isinstance(params.get("style"), dict) else {}


def _interchange_below_station(method: str, params: dict) -> bool:
    style = _style(params)
    if method != "map.build" or "preset" in style:
        return False
    numbers = {name: style.get(name, default) for name, default in
               (("station_radius", 4.2), ("interchange_radius", 6))}
    return all(serve._number(v) for v in numbers.values()) and \
        numbers["interchange_radius"] < numbers["station_radius"]


def _preset_beside_fields(method: str, params: dict) -> bool:
    style = _style(params)
    return method == "map.build" and "preset" in style and bool(set(style) - {"preset",
                                                                               "label_font"})


def _mode_loom_does_not_know(method: str, params: dict) -> bool:
    mode = params.get("mode") if isinstance(params, dict) else None
    return (method in ("graph.build", "feeds.add") and isinstance(mode, str)
            and bool(serve.MODE_PATTERN.fullmatch(mode)) and not feeds.valid_mode(mode))


def _not_a_regex(method: str, params: dict) -> bool:
    return method == "graph.build" and any(
        isinstance(params.get(name), str) and params[name] and not serve._regex(params[name])
        for name in ("label_pattern", "label_strip"))


def _source_not_a_url_or_absolute(method: str, params: dict) -> bool:
    source = params.get("source") if isinstance(params, dict) else None
    return (method == "feeds.add" and isinstance(source, str) and bool(source)
            and not source.startswith(("http://", "https://")) and not os.path.isabs(source))


def _only_whitespace(method: str, params: dict) -> bool:
    if method == "feeds.add":
        text = params.get("name")
    elif method == "export.encode" and isinstance(params.get("provenance"), dict):
        text = params["provenance"].get("alt")
    else:
        return False
    return isinstance(text, str) and bool(text) and not text.strip()


def _path_not_the_clients(method: str, params: dict) -> bool:
    return method == "export.encode" and any(
        isinstance(params.get(name), str) and params[name]
        and not os.path.isabs(params[name]) for name in ("source", "dest"))


def _a_list(method: str, params: dict) -> list | None:
    options = params.get("options") if method == "export.plan" else None
    board = options.get("storyboard") if isinstance(options, dict) else None
    return board if isinstance(board, list) else None


def _beside_a_list(method: str, params: dict) -> bool:
    """A view or a clock beside a list of beats on a video preset, whose
    first beat names its own (``export.BESIDE_A_LIST``)."""
    preset = export.PRESETS.get(params.get("preset")) if isinstance(params.get("preset"),
                                                                    str) else None
    return (_a_list(method, params) is not None and preset is not None
            and preset.kind == "video"
            and bool(params["options"].get("view") or params["options"].get("at")))


def _clock(text: str) -> float:
    hours, minutes, *seconds = (float(part) for part in text.split(":"))
    return hours * 3600 + minutes * 60 + sum(seconds)


def _beats_across(method: str, params: dict) -> bool:
    """The rules across a list's beats that StoryboardBeat's description
    states and the schema cannot hold, written from that description and
    not by asking ``export.authored_beats``, so that a rule it adds on one
    field, a bound the schema does not have, is not excused with them: the
    first beat names a view, has tween 0 or none, and names at unless it
    sweeps without hours; the beats last 90 seconds in all at most; a span's
    first clock is before its second; a card beat lasts 1 second at least
    and a draw_in beat 2; a list has one draw_in at most, which does not
    sweep, and it and every beat before it are on the geographic or map
    view, named or carried over. Asked only of params the schema accepts,
    so every beat has its fields' types."""
    board = _a_list(method, params)
    if not board:
        return False
    first = board[0]
    if (first.get("view") is None or first.get("tween") not in (None, 0)
            or (first.get("at") is None
                and not (first.get("sweep") and first.get("hours") is None))):
        return True
    if sum(beat["secs"] for beat in board) > 90:
        return True
    shown: list[str | None] = []
    draw_ins = 0
    for beat in board:
        span = beat.get("span")
        if span is not None and not _clock(span[0]) < _clock(span[1]):
            return True
        if beat.get("card") and beat["secs"] < 1:
            return True
        shown.append(beat["view"] if beat.get("view") is not None else shown[-1])
        if beat.get("draw_in"):
            draw_ins += 1
            if (draw_ins > 1 or beat["secs"] < 2 or beat.get("sweep")
                    or any(view not in ("geographic", "map") for view in shown)):
                return True
    return False


# Each rule by the sentence a refusal of it is written in, and when it applies.
SERVER_ONLY: dict[str, Callable[[str, Any], bool]] = {
    "interchange_radius may not be below station_radius": _interchange_below_station,
    "style.preset is sent alone or with label_font": _preset_beside_fields,
    "a mode is names gtfs2graph knows": _mode_loom_does_not_know,
    "label_pattern and label_strip are regular expressions": _not_a_regex,
    "feeds.add's source is a URL or an absolute path": _source_not_a_url_or_absolute,
    "text of only whitespace is refused": _only_whitespace,
    "export.encode's source and dest are absolute paths": _path_not_the_clients,
    "a list of beats stands without a view or a clock beside it": _beside_a_list,
    "the beats of a list are held to the rules across them": _beats_across,
}


def server_only(method: str, params: Any) -> str | None:
    if not isinstance(params, dict):
        return None
    for sentence, applies in SERVER_ONLY.items():
        if applies(method, params):
            return sentence
    return None


# ------------------------------------------------------------------- the test

@pytest.fixture
def client(monkeypatch):
    # A long method's work is never started: the answer to params the
    # server accepted is an empty result, before any feed is read.
    monkeypatch.setattr(serve.EngineEndpoint, "_job", lambda self, work: (lambda: {}))
    c = Client()
    yield c
    c.endpoint.close()


def verdict(client: Client, method: str, params: Any) -> tuple[bool | None, dict]:
    """Whether the server refuses ``params``, and its answer. The params
    error is code -32602 with kind ``params``; any other answer is the
    server's acceptance, a result or an error the work met once the params
    were judged, unless it is an error the engine does not mean: None, the
    server failed. pylsp_jsonrpc answers an exception it did not expect
    with code -32602 too, so the kind is what tells the two apart."""
    reply = client.call(method, params, timeout=30)
    error = reply.get("error")
    if error is None:
        return False, reply
    kind = (error.get("data") or {}).get("kind")
    if error["code"] == -32602 and kind == "params":
        return True, reply
    if error["code"] == serve.ENGINE_ERROR and kind in serve.KINDS:
        return False, reply
    return None, reply


def doubles_only(value: Any) -> bool:
    """Whether every number in ``value``, at any depth, is one a double
    holds: finite, and at most the largest double either side of zero."""
    if isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, int):
        return abs(value) <= sys.float_info.max
    if isinstance(value, dict):
        return all(doubles_only(inner) for inner in value.values())
    if isinstance(value, list):
        return all(doubles_only(inner) for inner in value)
    return True


def test_the_server_and_the_schema_agree_on_every_methods_params(client):
    """One test over every method, so that it can also hold ``SERVER_ONLY``
    to what it is for: each rule it names is met at least once, or it is
    an exception nothing needs any more."""
    assert sorted(BASES) == sorted(SCHEMA["methods"]), "every method has params to start from"
    disagreements: list[str] = []
    met: set[str] = set()
    tried = 0
    for method in sorted(SCHEMA["methods"]):
        validator = Draft202012Validator(
            {**SCHEMA["methods"][method]["params"], "$defs": DEFS},
            format_checker=Draft202012Validator.FORMAT_CHECKER)

        def judge(where: str, params: Any) -> None:
            nonlocal tried
            tried += 1
            outside = not doubles_only(params)
            schema_refuses = outside or not validator.is_valid(params)
            server_refuses, reply = verdict(client, method, params)
            if server_refuses is None:
                disagreements.append(f"{method} {where}: the server failed: "
                                     f"{reply['error']['message']}")
                return
            if schema_refuses == server_refuses:
                return
            rule = server_only(method, params) if server_refuses else None
            if rule is not None:
                met.add(rule)
                return
            side = ("the server refuses it" if server_refuses
                    else "the server takes a number no double holds" if outside
                    else "the schema refuses it")
            hint = (reply.get("error") or {}).get("data", {}).get("hint", "accepted")
            disagreements.append(f"{method} {where}: {side} ({hint})")

        for base in BASES[method]:
            server_refuses, reply = verdict(client, method, base)
            assert validator.is_valid(base) and server_refuses is False, (method, base, reply)
            for whole in (None, [], "zzz", 7, {"zzz": 1}):
                judge("params", whole)
            for path, value in cases(base, SCHEMA["methods"][method]["params"]):
                where = ".".join(str(part) for part in path)
                shown = "left out" if value is MISSING else repr(value)
                judge(f"{where} = {shown}", changed(base, path, value))
    # About 9,100 when this was written: a generator that shrinks without
    # anyone meaning it to fails here rather than passing on fewer cases.
    assert tried > 8000, f"only {tried} cases were tried"
    assert not disagreements, f"{len(disagreements)} of {tried}:\n" + "\n".join(disagreements)
    assert met == set(SERVER_ONLY), f"never met: {sorted(set(SERVER_ONLY) - met)}"


def test_a_field_left_out_of_a_plan_is_named_as_missing():
    """Each field CaptureJob requires, and each a beat of it requires, left
    out of a plan handed back: refused saying that field is missing, a
    nullable field and any other alike, rather than what a value of it must
    be. ``PLAN_FIELDS`` is the schema's list."""
    required = SCHEMA["$defs"]["CaptureJob"]["required"]
    assert list(serve.PLAN_FIELDS) == required
    plan = _plan_dict()
    for name in required:
        with pytest.raises(serve.JsonRpcInvalidParams) as refused:
            serve._capture_job({k: v for k, v in plan.items() if k != name})
        assert str(refused.value).startswith(f"plan.{name} is missing"), refused.value
    for name in SCHEMA["$defs"]["BeatPayload"]["required"]:
        beat = {k: v for k, v in plan["beats"][0].items() if k != name}
        with pytest.raises(serve.JsonRpcInvalidParams) as refused:
            serve._capture_job({**plan, "beats": [beat, *plan["beats"][1:]]})
        assert str(refused.value).startswith(f"plan.beats[0].{name} is missing"), refused.value


def test_a_null_date_is_told_it_may_be_left_out_where_it_may(client):
    """export.plan's date is optional: a null is refused saying it may be
    left out, not with map.build's sentence that the day is required."""
    plan = client.call("export.plan", {"key": KEY, "preset": "instagram-reel", "date": None})
    assert plan["error"]["data"]["hint"] == ("date must be a calendar day as YYYY-MM-DD, or "
                                             "left out"), plan
    drawn = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": None})
    assert drawn["error"]["data"]["hint"].startswith("date is required"), drawn
