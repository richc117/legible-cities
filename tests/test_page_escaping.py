"""A feed's text cannot end the page's data element.

The page carries its animation data inside a ``<script>`` element. A browser
does not read that element as JSON: it scans for the first ``</script`` and
ends the element there, whatever the surrounding text means. Station and line
names come from an agency's GTFS feed, so a name containing one would end the
data early and leave everything after it as live markup, in the desktop app
and on every page the site publishes.

These tests are about the substitution, not about drawing, so they build the
page's text directly rather than running the pipeline.
"""

import json

from schematic.animate import _HTML, _json_for_script

HOSTILE = '</script><img src=x onerror="window.__owned=1">'


def _data_element(page: str) -> str:
    """What a browser would take as the data element's contents."""
    start = page.index('<script id="data"')
    body = page.index(">", start) + 1
    return page[body:page.index("</script>", body)]


def test_a_closing_tag_in_a_name_does_not_end_the_element():
    page = _HTML.replace("__DATA__", _json_for_script({"lines": [{"label": HOSTILE}]}))
    element = _data_element(page)
    # The whole payload is inside the element, and parses.
    assert json.loads(element) == {"lines": [{"label": HOSTILE}]}


def test_the_name_survives_the_round_trip_exactly():
    for name in [
        HOSTILE,
        "</SCRIPT >",
        "</script\t",
        "<!--",
        "Ampersand & Co",
        "Metro A Line",
        "Gare du Nord",
        "駅",
    ]:
        assert json.loads(_json_for_script({"n": name}))["n"] == name


def test_nothing_that_could_end_an_element_survives_the_escape():
    out = _json_for_script({"a": HOSTILE, "b": "<!-- --> & <script>"})
    for character in "<>&":
        assert character not in out, f"{character!r} reached the page unescaped"


def test_the_unescaped_form_would_have_broken_out():
    """The defect this guards against, stated so the guard is not mistaken
    for decoration: the plain encoder leaves the element ending early."""
    plain = json.dumps({"lines": [{"label": HOSTILE}]}, separators=(",", ":"))
    page = _HTML.replace("__DATA__", plain)
    assert _data_element(page) != plain
    assert "<img src=x" in page[page.index("</script>", page.index('<script id="data"')):]


def test_ordinary_data_is_unchanged_apart_from_the_escapes():
    data = {"trips": [{"line": "A", "at": 1.5}], "labels": True}
    assert json.loads(_json_for_script(data)) == data
