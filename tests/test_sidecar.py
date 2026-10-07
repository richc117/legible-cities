"""The sidecar's ``alt``: the engine's sentence unless the caller wrote its own.

``Provenance.alt`` lets a person give the description of a file in their own
words (issue 41). Only the sidecar can change, and only when it is given: the
two pins below hold the omitted case to the bytes the release before wrote.
"""

import hashlib
import json

import pytest
from test_serve import Client, check, invalid

from schematic import export, serve

KEY = "la-metro-rail"

# All five fields the caller can give, so the text is the same where the
# atlas's networks.json and the built maps exist (the main checkout) and where
# they do not (a worktree): a sidecar written without them differs between the two.
PROVENANCE = {"service_date": "2026-09-11", "trips": 1254, "stations": 101, "lines": 6,
              "caveats": ["a caveat"]}

# A still and a storyboard, each with the sha256 of the sidecar the release
# before wrote for a ten-byte file.
SIDECARS = [
    pytest.param("instagram-post", "x.png", {"view": "map"},
                 "e756f06b9125d4d2a757c842a50089842624becd83c5c09a0b2e51d5841fff07",
                 id="still"),
    pytest.param("instagram-reel", "y.mp4", {"view": "map", "storyboard": "tour"},
                 "4edaca6868b3675b981a1096968215a18acc5589cc3306851f00e21d3a652fe9",
                 id="storyboard"),
]


def sidecar(tmp_path, preset, name, extra, **given):
    """The sidecar bytes ``_write_sidecar`` writes beside a ten-byte file."""
    path = tmp_path / name
    path.write_bytes(b"0123456789")
    export._write_sidecar(KEY, export.PRESETS[preset], [path], theme="dark",
                          provenance=PROVENANCE, **extra, **given)
    return export.sidecar_path(path).read_bytes()


@pytest.fixture
def client():
    c = Client()
    yield c
    c.endpoint.close()


@pytest.mark.parametrize("preset,name,extra,pin", SIDECARS)
def test_without_an_alt_the_sidecar_is_the_one_the_release_before_wrote(
        tmp_path, preset, name, extra, pin):
    data = sidecar(tmp_path, preset, name, extra)
    assert hashlib.sha256(data).hexdigest() == pin
    assert json.loads(data)["alt"].startswith(("Schematic map of", "An animation of"))


@pytest.mark.parametrize("preset,name,extra,pin", SIDECARS)
def test_a_given_alt_replaces_the_alt_and_nothing_else(tmp_path, preset, name, extra, pin):
    own = "A person's own words."
    omitted = json.loads(sidecar(tmp_path, preset, name, extra))
    given = json.loads(sidecar(tmp_path, preset, name, extra, alt=own))
    assert given["alt"] == own != omitted["alt"]
    assert given == {**omitted, "alt": own}
    assert list(given) == list(omitted)


def test_an_alt_is_trimmed_over_the_protocol_and_written_beside_the_file(client, tmp_path):
    """The whole way: ``export.encode`` as the app calls it, a still the preset
    already is (so it is copied and no ffmpeg runs), the alt with the spaces and
    the newline a text box leaves on it."""
    job = export.plan(KEY, "instagram-post", quality="draft")
    assert job.mode == "still" and job.keep
    source = tmp_path / "capture.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not a real picture")
    dest = tmp_path / "out" / job.filename
    answer = client.call("export.encode", {
        "plan": {**job.to_dict(), "filename": job.filename}, "source": str(source),
        "dest": str(dest), "provenance": {"alt": "  A person's own words.\n"}})
    assert "error" not in answer, answer
    result = answer["result"]
    check(result, "ExportEncodeResult")
    assert dest.read_bytes() == source.read_bytes()
    assert result["sidecar"]["alt"] == "A person's own words."
    beside = json.loads(export.sidecar_path(dest).read_text(encoding="utf-8"))
    assert beside["alt"] == "A person's own words."
    assert beside == result["sidecar"]


def refusal(alt):
    with pytest.raises(serve.JsonRpcInvalidParams) as raised:
        serve._provenance({"alt": alt})
    assert raised.value.data["kind"] == "params"
    return raised.value.data["hint"]


def test_the_server_keeps_an_alt_of_a_thousand_characters_whole_and_refuses_the_rest():
    whole = "x" * 1000
    assert serve._provenance({"alt": whole}) == {"alt": whole}
    # Characters are code points, which is what the schema's maxLength counts: a
    # JavaScript string's length would call each of these two.
    emoji = "\U0001F5FA" * 1000
    assert serve._provenance({"alt": emoji}) == {"alt": emoji}
    # The bound is judged on the text as sent, before it is trimmed.
    assert "1,001" in refusal("x" * 1001)
    assert "1,000" in refusal("x" * 1001)
    assert "1,000" in refusal(" " + "x" * 1000)
    for blank in ("", "  \n ", "\t"):
        assert "omit it instead" in refusal(blank)
    assert refusal(7) == "provenance.alt must be text"
    assert refusal(True) == "provenance.alt must be text"
    assert refusal(["a"]) == "provenance.alt must be text"
    # Not given is not refused, here as for every other field.
    assert serve._provenance({"alt": None}) == {}
    assert serve._provenance({}) == {}


def test_the_server_trims_an_alt_and_changes_nothing_else_in_it():
    inner = "Line one.\n\n  Line  two, <b>bold</b> & \"quoted\".\té"
    assert serve._provenance({"alt": f" \n{inner}\t  "}) == {"alt": inner}


def test_the_schema_agrees_with_the_server_where_it_can():
    """The schema holds the shape and the server holds the rest. A text of only
    whitespace is valid to the schema (``minLength`` counts its characters) and
    refused by the server, which trims first; that one gap is deliberate and
    the field's description says so."""
    alt = serve.schema()["$defs"]["Provenance"]["properties"]["alt"]
    assert alt["type"] == "string"
    assert alt["minLength"] == 1 and alt["maxLength"] == 1000
    assert not invalid({"alt": "x" * 1000}, "Provenance")
    assert not invalid({"alt": "\U0001F5FA" * 1000}, "Provenance")
    assert not invalid({"alt": "x"}, "Provenance")
    assert invalid({"alt": ""}, "Provenance")
    assert invalid({"alt": "x" * 1001}, "Provenance")
    assert invalid({"alt": 7}, "Provenance")
    assert not invalid({"alt": "  \n "}, "Provenance")
    assert "omit it instead" in refusal("  \n ")
