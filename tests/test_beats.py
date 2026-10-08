"""A storyboard written as a list of beats, and a named one opened where asked
(issues 39 and 31).

``export.plan``'s ``storyboard`` is a name or a list a client wrote. A list
is checked beat by beat and plans the way a name does; a name given ``view``
or ``at`` opens on them with no transition, so the page's address and frame 0
agree. Nothing a name planned before moves, and ``beat_payload``, which the
recorder and the determinism test share, is not where any of it happens.

All pure: ``export.plan`` reads the registry, which is code, so nothing here
needs a feed, a layout, a browser or ffmpeg.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from unittest import mock
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import pytest
from pylsp_jsonrpc.exceptions import JsonRpcInvalidParams

from schematic import export, feeds, serve
from test_serve import Client, check, invalid

KEY = "la-metro-rail"
PAGE = "app://local/projects/p1/la-metro-rail.html"
DATE = "2026-09-11"


@pytest.fixture
def client():
    c = Client()
    yield c
    c.endpoint.close()


def _query(job: export.CaptureJob) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(job.url).query).items()}


def _opted_out():
    """The registry with one feed opted out of the geometry, as
    test_export.py makes it: every registered feed carries it now."""
    plain = "opted-out"
    real = feeds.FEEDS[KEY]
    return plain, mock.patch.dict(
        feeds.FEEDS, {**feeds.FEEDS, plain: replace(real, key=plain, geographic=False)},
        clear=True)


# ------------------------------------------------------- the names, unmoved

# sha256 of json.dumps(plan.to_dict(), sort_keys=True), taken at the v0.11.0
# release commit before any of this was written: each name on portfolio-mp4,
# and each video preset with no storyboard given, on the app's page address
# and a fixed service day so the pin holds on every machine.
BEFORE = {
    ("portfolio-mp4", "transform"):
        "3ff8df8061b92dc7b5fd1daea5d8329f7ad2da9cebfd20341041a088271748be",
    ("portfolio-mp4", "transform-loop"):
        "cfa280e0e6aa2b5a802f1cdb3eab503333f51ea8d2d1056416d09ac9c0fc2a71",
    ("portfolio-mp4", "essay-loop"):
        "ddb23ce1de2fa766da88a36984768478fc3aae326f0ab83e5d745c9adda9e37f",
    ("portfolio-mp4", "tour"):
        "ea7264a3945e4341cf95146deeb890fe2b04cdbca2d03d428211d7d86edb2867",
    ("portfolio-mp4", "reveal"):
        "9b0e4284012ad98c6ceb931a5cd79446f9fa63afe919994c858470afafbb9138",
    ("portfolio-mp4", "morph"):
        "0005ad2fe3c045061dc968884a4bc00532cebfac8922a963b1868ad71eeb208f",
    ("portfolio-mp4", "day"):
        "367b5a7b507ef2ed63999e3837ee2db920e9b107d5751e1176cbb4a06a8b376e",
    ("portfolio-mp4", "run"):
        "dfe4d034cdac9bd9ffc4bdaa82d060d67e2cbf8ff8fd061497c9c00b10e3e6d9",
    ("instagram-reel", None):
        "bd7d5978ffbbbc7bd9dea517978a7f60f11dfa6e8d5f29ec6f092145271cd8ea",
    ("bluesky-video", None):
        "c47b9ce868415a151c3550a82bf180c0bf0aca2345b093be93bc926283c32e6b",
    ("linkedin-video", None):
        "97368c0dca6125352960ae8fe4a752fd03a2971ca523cc401ab645cc1262390f",
    ("instagram-reel-gif", None):
        "ceae9bec8595d6420a29817a75010072b2ebe0f2ca6ba6511796ab58be90c0c4",
    ("linkedin-gif", None):
        "d8f4120d1efb6ba4dc56168fdef02dde9cd78d979d5cd6bba91ad71f56e0f4a0",
    ("bluesky-gif", None):
        "9a41fc0e6677f8a6ed8bba384a9aab10199ee8587062d0a33e30d28230c1b639",
    ("portfolio-mp4", None):
        "ea7264a3945e4341cf95146deeb890fe2b04cdbca2d03d428211d7d86edb2867",
    ("portfolio-gif", None):
        "a042c6f76080e1fde182f6750fe5bee00cb3a2545ffd92200a3d1894baeef8a7",
}


def test_the_pins_cover_every_name_and_every_video_preset():
    assert {s for _, s in BEFORE if s} == set(export.STORYBOARDS)
    assert {p for p, s in BEFORE if s is None} == {
        k for k, p in export.PRESETS.items() if p.kind == "video"}


# What issue 40 (lane E40) added to every plan, and to the address of a preset
# with safe zones. The pins compare the beats, the view, the clock and the
# address to v0.11.0; the caption, the corner and the zones are E40's
# additions, judged by tests/test_overlay.py, so they are left out of the hash.
E40_FIELDS = ("caption", "clock_corner")
E40_QUERY = ("corner", "ztop", "zbottom", "zside", "zrail", "zrailtop")


def _as_before_e40(plan: dict) -> dict:
    """The plan without E40's two fields, and its address without E40's
    keys: the query parsed and re-encoded as url_for writes it, so the order
    and the encoding of every other key are untouched."""
    plan = {k: v for k, v in plan.items() if k not in E40_FIELDS}
    base, query = plan["url"].split("?", 1)
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k not in E40_QUERY]
    plan["url"] = base + "?" + urlencode(kept)
    return plan


@pytest.mark.parametrize("preset,storyboard", list(BEFORE))
def test_the_eight_names_plan_as_the_release_before(preset, storyboard):
    """A name asked for without ``view`` or ``at`` plans byte for byte what
    v0.11.0 planned: the same beats, address and job."""
    job = export.plan(KEY, preset, storyboard=storyboard, page=PAGE, date=DATE)
    plan = _as_before_e40(job.to_dict())
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    assert digest == BEFORE[(preset, storyboard)]


# ------------------------------------------------- a name opened where asked


def test_a_named_storyboard_opens_on_the_view_and_clock_asked_for():
    """Issue 31: ``view: linear, at: 06:30`` on the reel opened in linear and
    then morphed to tour's map over 1.2 seconds, and seeked to 05:30 before
    frame 0. Now the first beat is the one asked for, with no morph, and the
    address says what the first beat says."""
    job = export.plan(KEY, "instagram-reel", view="linear", at="06:30")
    assert job.beats[0] == {"secs": 6, "view": "linear", "labels": None, "at": 23400.0,
                            "speed": 240, "sweep": False, "hours": None, "lo": 0.0,
                            "hi": 86400.0, "tween": 0}
    assert job.beats[1:] == tuple(export.beat_payload(export.STORYBOARDS["tour"]))[1:]
    assert job.storyboard == "tour"
    query = _query(job)
    assert (query["view"], query["at"]) == ("linear", "06:30")
    assert job.view == "linear" and job.at == 23400.0


def test_a_view_alone_or_a_clock_alone_keeps_the_rest_of_the_opening_beat():
    view_only = export.plan(KEY, "instagram-reel", view="time")
    assert (view_only.beats[0]["view"], view_only.beats[0]["at"]) == ("time", 19800.0)
    assert _query(view_only)["at"] == "05:30" and view_only.at == 19800.0
    at_only = export.plan(KEY, "instagram-reel", at="09:00")
    assert (at_only.beats[0]["view"], at_only.beats[0]["at"]) == ("map", 32400.0)
    assert at_only.beats[0]["tween"] == 0 and at_only.view == "map"


def test_the_rewritten_beat_and_a_list_are_judged_for_geography():
    """``check_geographic`` sees the beats that will be played: a reel asked
    to open on geography, and a list that visits it, on a feed without it."""
    plain, opted_out = _opted_out()
    with opted_out:
        with pytest.raises(ValueError, match="no geographic geometry"):
            export.plan(plain, "instagram-reel", view=export.GEO_VIEW)
        with pytest.raises(ValueError, match="no geographic geometry"):
            export.plan(plain, "instagram-reel",
                        storyboard=[{"secs": 2, "view": "map", "at": "08:00"},
                                    {"secs": 2, "view": export.GEO_VIEW}])
        # Neither asks for it here, and both plan.
        export.plan(plain, "instagram-reel", view="linear")
        export.plan(plain, "instagram-reel",
                    storyboard=[{"secs": 2, "view": "map", "at": "08:00"},
                                {"secs": 2, "view": "time"}])


# ------------------------------------------------------------------ a list


TWO = [{"secs": 6, "view": "linear", "at": "06:30", "speed": 240}, {"secs": 3, "view": "time"}]


def test_a_list_plans_like_a_name():
    job = export.plan(KEY, "instagram-reel", storyboard=TWO)
    assert job.beats == tuple(export.beat_payload(export.authored_beats(TWO)))
    assert job.storyboard == export.CUSTOM == "custom"
    assert export.CUSTOM not in export.STORYBOARDS
    query = _query(job)
    assert (query["view"], query["at"]) == ("linear", "06:30")
    assert job.view == "linear" and job.at == 23400.0


def test_day_written_as_a_list_plans_days_beats_and_its_sweep_note():
    """The same beats as the name, but for the opening beat's tween: a name
    keeps the default ``beat_payload`` gives it, and a list opens with none.
    The sweep too fast to read is said of a list as of a name."""
    as_list = [{"secs": b.secs, "view": b.view, "labels": b.labels, "at": b.at,
                "speed": b.speed, "sweep": b.sweep, "hours": b.hours, "span": b.span,
                "tween": b.tween} for b in export.STORYBOARDS["day"]]
    named = export.plan(KEY, "portfolio-mp4", storyboard="day")
    written = export.plan(KEY, "portfolio-mp4", storyboard=as_list)
    assert named.beats[0]["tween"] == 1 and written.beats[0]["tween"] == 0
    assert written.beats == ({**named.beats[0], "tween": 0},) + named.beats[1:]
    assert named.notes and written.notes == named.notes
    assert "simulated seconds per frame" in written.notes[0]


def test_a_list_of_beats_is_taken_as_a_list_of_objects():
    """``plan`` may be handed ``Beat``s in Python; it checks them all the same."""
    beats = (export.Beat(3, view="map", at="08:00", tween=0), export.Beat(3, view="linear"))
    assert export.authored_beats(beats) == beats
    job = export.plan(KEY, "instagram-reel", storyboard=beats)
    assert job.beats == tuple(export.beat_payload(beats))
    with pytest.raises(ValueError, match=r"storyboard\[0\]\.tween"):
        export.plan(KEY, "instagram-reel",
                    storyboard=(export.Beat(3, view="map", at="08:00", tween=1),))
    with pytest.raises(ValueError, match=r"storyboard\[0\]\.at"):
        export.plan(KEY, "instagram-reel", storyboard=(export.Beat(3, view="map"),))


# --------------------------------------------------------------- the first beat


def _refused(beats) -> str:
    """The sentence ``_export_options`` refuses a list with, as params."""
    with pytest.raises(JsonRpcInvalidParams) as caught:
        serve._export_options({"storyboard": beats})
    assert caught.value.data["kind"] == "params"
    return caught.value.data["hint"]


def test_the_first_beat_names_a_view():
    hint = _refused([{"secs": 3}, {"secs": 3, "view": "map"}])
    assert "storyboard[0]" in hint and "view" in hint


def test_the_first_beat_does_not_transition_into_it():
    hint = _refused([{"secs": 3, "view": "map", "at": "08:00", "tween": 0.5}])
    assert "storyboard[0].tween" in hint and "frame 0" in hint


@pytest.mark.parametrize("first", [
    pytest.param({"secs": 3, "view": "time"}, id="no-clock"),
    pytest.param({"secs": 3, "view": "map", "sweep": True, "hours": 4}, id="sweeps-hours"),
    pytest.param({"secs": 3, "view": "map", "span": ["06:00", "09:00"]}, id="span-no-sweep"),
])
def test_the_first_beat_sets_the_clock(first):
    """Two captures of one plan have to agree, and a first beat that names no
    time starts wherever the page's clock happened to be: the desktop app's
    capture refuses that plan, so the engine does not make it."""
    hint = _refused([first, {"secs": 3, "view": "linear"}])
    assert "storyboard[0].at" in hint and "reproducible" in hint
    with pytest.raises(ValueError, match=re.escape("storyboard[0].at")):
        export.plan(KEY, "instagram-reel", storyboard=[first])


@pytest.mark.parametrize("first", [
    pytest.param({"secs": 10, "view": "map", "sweep": True}, id="sweeps-the-day"),
    pytest.param({"secs": 10, "view": "map", "sweep": True, "span": ["06:00", "09:00"]},
                 id="sweeps-a-span"),
])
def test_a_first_beat_that_sweeps_a_span_sets_the_clock_itself(first):
    """The app's one exception: a sweep given its span, not a number of hours,
    carries ``lo`` and ``hi`` to the recorder, which seeks to ``lo``."""
    job = export.plan(KEY, "instagram-reel", storyboard=[first, {"secs": 3, "view": "linear"}])
    opening = job.beats[0]
    assert opening["at"] is None and opening["sweep"] is True
    assert opening["lo"] is not None and opening["hi"] is not None
    assert opening["hi"] >= opening["lo"] >= 0
    assert job.at is None and "at" not in _query(job)


def test_only_the_first_beats_tween_is_read_as_zero():
    beats = export.authored_beats([{"secs": 3, "view": "map", "at": "08:00"},
                                   {"secs": 3, "view": "linear"},
                                   {"secs": 3, "view": "time", "tween": 0.5}])
    assert beats[0].tween == 0
    assert beats[1].tween is None and beats[2].tween == 0.5
    job = export.plan(KEY, "instagram-reel", storyboard=beats)
    assert [b["tween"] for b in job.beats] == [0, 1.2, 0.5]
    # A null tween on the first beat is read the same as an absent one.
    assert export.authored_beats(
        [{"secs": 3, "view": "map", "at": "08:00", "tween": None}])[0].tween == 0


# -------------------------------------------------------------- every bound

OK = {"secs": 1, "view": "map", "at": "08:00"}

# Each hint names the beat and the field as one path, ``storyboard[1].at``, so
# a two-letter field cannot pass by appearing anywhere in the sentence.
BOUNDS = [
    pytest.param([OK, {"secs": 0.4}], "storyboard[1].secs", "", id="secs-0.4"),
    pytest.param([OK, {"secs": 30.5}], "storyboard[1].secs", "", id="secs-30.5"),
    pytest.param([{"secs": 30, "view": "map", "at": "08:00"}, {"secs": 30}, {"secs": 30},
                  {"secs": 0.5}], "storyboard[3].secs", "90 seconds", id="total-90.5"),
    pytest.param([OK] * 17, "storyboard[16]", "16 beats", id="seventeen"),
    pytest.param([OK, {"secs": 1, "sweep": True, "hours": 0}], "storyboard[1].hours", "",
                 id="hours-0"),
    pytest.param([OK, {"secs": 1, "sweep": True, "hours": 24.5}], "storyboard[1].hours", "",
                 id="hours-24.5"),
    pytest.param([OK, {"secs": 1, "speed": -1}], "storyboard[1].speed", "", id="speed--1"),
    pytest.param([OK, {"secs": 1, "sweep": True, "span": ["09:00", "08:00"]}],
                 "storyboard[1].span", "", id="span-backwards"),
    pytest.param([OK, {"secs": 1, "sweep": True, "span": ["09:00", "09:00"]}],
                 "storyboard[1].span", "", id="span-empty"),
    pytest.param([OK, {"secs": 1, "at": "7am"}], "storyboard[1].at", "", id="at-7am"),
    pytest.param([OK, {"secs": 1, "view": "plan"}], "storyboard[1].view", "", id="view-plan"),
    pytest.param([OK, {"secs": 1, "labels": "yes"}], "storyboard[1].labels", "",
                 id="labels-yes"),
    pytest.param([OK, {"secs": 1, "sweep": "yes"}], "storyboard[1].sweep", "",
                 id="sweep-yes"),
    pytest.param([OK, {"secs": 1, "tween": -1}], "storyboard[1].tween", "", id="tween--1"),
    pytest.param([OK, {"secs": 1, "lo": 0}], "storyboard[1] does not take lo", "",
                 id="unknown-lo"),
]


@pytest.mark.parametrize("beats,path,also", BOUNDS)
def test_every_bound_is_refused_by_beat_and_field(beats, path, also):
    hint = _refused(beats)
    assert path in hint and also in hint, hint


def test_ninety_seconds_is_the_ceiling_and_plans_2700_frames():
    three = [{"secs": 30, "view": "map", "at": "08:00"}, {"secs": 30}, {"secs": 30}]
    assert serve._export_options({"storyboard": three})["storyboard"] == \
        export.authored_beats(three)
    plan = export.plan(KEY, "instagram-reel", storyboard=three)
    assert sum(round(b["secs"] * plan.fps) for b in plan.beats) == 2700


def test_each_bound_on_its_edge_is_taken():
    export.authored_beats([{"secs": 0.5, "view": "map", "at": "00:00", "speed": 0},
                           {"secs": 30, "sweep": True, "hours": 24},
                           {"secs": 1, "sweep": True, "span": ["08:59", "09:00"]},
                           {"secs": 1, "tween": 0, "labels": False, "at": "25:44"}])


# ------------------------------------------------------ a clock read whole

@pytest.mark.parametrize("beats,path", [
    pytest.param([{"secs": 2, "view": "map", "at": "06:30\n"}], "storyboard[0].at",
                 id="first-beats-at"),
    pytest.param([OK, {"secs": 1, "at": "09:00\n"}], "storyboard[1].at", id="later-beats-at"),
    pytest.param([OK, {"secs": 1, "sweep": True, "span": ["08:00", "09:00\n"]}],
                 "storyboard[1].span", id="span-end"),
    pytest.param([OK, {"secs": 1, "sweep": True, "span": ["08:00\n", "09:00"]}],
                 "storyboard[1].span", id="span-start"),
])
def test_a_beats_clock_with_a_trailing_newline_is_refused(client, beats, path):
    """Issue 57: ``$`` also matches before a final newline, so a clock read with
    ``match`` took ``"06:30\\n"`` and planned it as 06:30 where the app's validators
    refuse it. The beat's ``at`` and the two ends of a ``span`` now read ``Clock``
    whole, as the ``at`` option beside a name already did. Not a ``BOUNDS`` row:
    those are judged on the sentence alone, and this one is judged over the method too."""
    hint = _refused(beats)
    assert path in hint, hint
    params = {"key": KEY, "preset": "instagram-reel", "options": {"storyboard": beats}}
    error = client.call("export.plan", params)["error"]
    assert error["code"] == -32602, error
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "params"
    assert path in error["data"]["hint"], error
    with pytest.raises(ValueError, match=re.escape(path)):
        export.authored_beats(beats)


# ------------------------------------------------------------ beside a list


@pytest.mark.parametrize("beside", [{"view": "time"}, {"at": "08:00"}])
def test_view_or_at_beside_a_list_is_refused_for_a_video(client, beside):
    params = {"key": KEY, "preset": "instagram-reel",
              "options": {"storyboard": TWO, **beside}}
    error = client.call("export.plan", params)["error"]
    assert error["code"] == -32602, error
    check(error["data"], "ErrorData")
    assert error["data"]["kind"] == "params"
    assert "storyboard[0]" in error["data"]["hint"]
    with pytest.raises(ValueError, match=re.escape("storyboard[0]")):
        export.plan(KEY, "instagram-reel", storyboard=TWO, **beside)


def test_a_still_ignores_a_list_and_keeps_its_own_view(client):
    """The desktop app keeps one storyboard per project and may send it with
    a still; the still is taken in the view asked for."""
    result = client.call("export.plan", {
        "key": KEY, "preset": "instagram-post",
        "options": {"storyboard": TWO, "view": "time", "at": "08:00"}})["result"]
    check(result, "CaptureJob")
    assert result["storyboard"] == "" and result["beats"] == []
    assert result["view"] == "time" and result["at"] == 8 * 3600


# ------------------------------------------------------- the plan comes back


def test_a_lists_plan_comes_back_as_custom(client):
    result = client.call("export.plan", {"key": KEY, "preset": "instagram-reel",
                                         "options": {"storyboard": TWO}})["result"]
    check(result, "CaptureJob")
    assert result["storyboard"] == "custom"
    job = serve._capture_job(result)
    assert job.storyboard == "custom"
    assert job.beats == tuple(export.beat_payload(export.authored_beats(TWO)))
    assert "custom" not in export.STORYBOARDS


# ---------------------------------------------------- described by its views


@pytest.mark.parametrize("name", list(export.STORYBOARDS))
def test_a_name_and_its_own_beats_are_described_alike(name):
    beats = export.STORYBOARDS[name]
    payload = export.beat_payload(beats)
    for given in (beats, payload, list(beats)):
        assert export.storyboard_views(given) == export.storyboard_views(name)
        assert (export.storyboard_alt(KEY, given, stations=9, lines=3)
                == export.storyboard_alt(KEY, name, stations=9, lines=3))
        assert export.wants_geographic(storyboard=given) == \
            export.wants_geographic(storyboard=name)


def test_a_list_is_described_by_its_views_and_named_by_none(tmp_path):
    beats = export.authored_beats([{"secs": 3, "view": "linear", "at": "08:00"},
                                   {"secs": 3, "view": "time"}, {"secs": 3, "view": "linear"}])
    payload = tuple(export.beat_payload(beats))
    assert export.storyboard_views(beats) == "linear -> time"
    assert export.storyboard_views(payload) == "linear -> time"
    alt = export.storyboard_alt(KEY, payload, stations=9, lines=3)
    assert alt == export.storyboard_alt(KEY, beats, stations=9, lines=3)
    assert export.VIEW_PHRASE["linear"] in alt and export.VIEW_PHRASE["time"] in alt
    assert alt.index(export.VIEW_PHRASE["linear"]) < alt.index(export.VIEW_PHRASE["time"])
    # Past the sentence every animation's alt is framed in, no storyboard is named.
    own = alt.replace("running a real day's timetable", "")
    for name in (export.CUSTOM, *export.STORYBOARDS):
        assert not re.search(rf"\b{re.escape(name)}\b", own), name

    # And the sidecar beside the file says the same.
    path = tmp_path / "la-metro-rail-instagram-reel.mp4"
    path.write_bytes(b"\0")
    export._write_sidecar(KEY, export.PRESETS["instagram-reel"], [path], theme="dark",
                          view="linear", storyboard="custom", beats=payload,
                          provenance={"stations": 9, "lines": 3})
    meta = json.loads(export.sidecar_path(path).read_text(encoding="utf-8"))
    assert meta["storyboard"] == "custom"
    assert meta["view"] == "linear -> time"
    assert meta["alt"] == alt


def test_encode_describes_a_list_by_the_beats_it_planned(tmp_path, monkeypatch):
    """``encode`` hands the plan's beats to the sidecar; ffmpeg is stood in
    for, since what is checked is the file beside the video."""
    monkeypatch.setattr(export, "_encode",
                        lambda source, dest, preset, **kw: dest.write_bytes(b"\0"))
    job = export.plan(KEY, "instagram-reel", storyboard=TWO)
    dest = tmp_path / "out" / job.filename
    export.encode(job, tmp_path / "frames", dest, provenance={"stations": 9, "lines": 3})
    meta = json.loads(export.sidecar_path(dest).read_text(encoding="utf-8"))
    assert (meta["storyboard"], meta["view"]) == ("custom", "linear -> time")
    assert meta["alt"] == export.storyboard_alt(KEY, job.beats, stations=9, lines=3)
    # A name opened elsewhere is described by what it plays, too.
    opened = export.plan(KEY, "instagram-reel", view="linear")
    export.encode(opened, tmp_path / "frames", dest, provenance={"stations": 9, "lines": 3})
    meta = json.loads(export.sidecar_path(dest).read_text(encoding="utf-8"))
    assert (meta["storyboard"], meta["view"]) == ("tour", "linear -> time")


# ----------------------------------------------------------------- schema


def test_the_schema_takes_a_list_and_a_custom_plan():
    for beats in (TWO, [{"secs": 1, "view": "map"}], [{"secs": 30, "view": "map"}] * 3):
        check({"storyboard": beats}, "ExportOptions")
    check({"storyboard": "tour", "view": "linear", "at": "06:30"}, "ExportOptions")
    assert invalid({"storyboard": [{"secs": 1, "view": "map", "tween": -1}]}, "ExportOptions")
    plan = export.plan(KEY, "instagram-reel", storyboard=TWO)
    check({**plan.to_dict(), "filename": plan.filename}, "CaptureJob")
    # export.storyboards' own beats, every field present, are still beats.
    for row in export.storyboard_table():
        for beat in row["beats"]:
            check(beat, "StoryboardBeat")


# ---------------------------------------------------------------- the preset


def test_bluesky_video_allows_what_bluesky_allows():
    """Bluesky's client takes 300 MB a video (read 6 Oct 2026); 50 MB refused
    a long export at high quality after its capture had run."""
    assert export.PRESETS["bluesky-video"].max_bytes == 300_000_000
