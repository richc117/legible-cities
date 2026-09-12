"""A feed's own colours cannot become markup (E24).

The map is drawn from an agency's GTFS file. A line's stroke is that file's
``route_color``, and it used to be interpolated into the ``stroke`` attribute
exactly as published -- on the same line as the label beside it, which *was*
escaped. So a ``route_color`` of ``"><script>`` closed the attribute, closed
the element and opened one of the feed's choosing: in the desktop app's
frame, in the animation page, and on every map the public site publishes.

Two halves, and both are needed. The value is held to the six hex digits the
spec asks for, falling back to the default the way a missing colour already
did; and the interpolation is escaped regardless, because a renderer should
not depend on a validator elsewhere in the file being right.

``feeds.inspect`` carries the same two columns straight out to a client, so
it is held to the same shape here.
"""

import xml.etree.ElementTree as ET

import pytest
from test_colors import _graph, strokes
from test_feeds import GOOD, gtfs_zip

from schematic import config, feeds
from schematic.render import Style, render

DEFAULT = Style().default_line_color

# Each ends the attribute it lands in and starts something else.
HOSTILE = [
    '"><script>alert(1)</script><g stroke="#000000',
    'red" onload="alert(1)',
    '#0072bc"><img src=x onerror=alert(1)>',
    "'><svg onload=alert(1)>",
]


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV, str(tmp_path))
    return tmp_path


# --------------------------------------------------------------- the map

@pytest.mark.parametrize("color", HOSTILE)
def test_a_hostile_route_color_draws_the_default_and_writes_no_markup(color):
    svg = render(_graph([[("A", color)], [("B", "0072bc")]]), labels=False).svg
    assert strokes(svg) == {"A": DEFAULT, "B": "#0072bc"}
    # Not "escaped into the attribute" -- not in the document at all, because
    # the value never became a colour.
    assert "script" not in svg and "onload" not in svg and "onerror" not in svg
    assert "<g" not in svg[svg.index('data-line="A"'):svg.index("</g>")]


@pytest.mark.parametrize("color", HOSTILE)
def test_the_document_still_parses_whatever_the_feed_published(color):
    svg = render(_graph([[("A", color), ("B", color)]]), labels=False).svg
    root = ET.fromstring(svg)
    lines = root.find("{http://www.w3.org/2000/svg}g[@id='lines']")
    assert [g.get("stroke") for g in lines] == [DEFAULT, DEFAULT]


@pytest.mark.parametrize("color,drawn", [
    ("0072bc", "#0072bc"),          # as GTFS asks for it
    ("#0072BC", "#0072BC"),         # as feeds write it anyway
    ("AABBCC", "#AABBCC"),
    ("red", DEFAULT),               # a name is not six hex digits
    ("#12345", DEFAULT),
    ("#1234567", DEFAULT),
    ("0072bc ", DEFAULT),
    ("", DEFAULT),
    (None, DEFAULT),
])
def test_the_feeds_colour_is_held_to_the_same_six_digits_a_caller_is(color, drawn):
    assert strokes(render(_graph([[("A", color)]]), labels=False).svg) == {"A": drawn}


def test_the_escape_holds_even_if_a_colour_gets_past_the_check():
    """Belt and braces: an override reaches the stroke through ``check_color``
    and a Style's furniture through neither, so the emission escapes too. If
    a validator is ever loosened, the document still cannot be broken out of.
    """
    graph = _graph([[("A", "0072bc")]])
    svg = render(graph, style=Style(background='#fff" onload="alert(1)'), labels=True).svg
    assert 'onload="alert(1)' not in svg
    assert "&quot; onload=&quot;alert(1)" in svg
    ET.fromstring(svg)


def test_the_unescaped_form_would_have_broken_out():
    """The defect this guards against, stated so the guard is not mistaken
    for decoration."""
    hostile = '"><script>alert(1)</script><g stroke="#000000'
    plain = f'<g class="line" data-line="A" stroke="{hostile}" stroke-width="7.00">'
    assert "<script>" in plain
    with pytest.raises(ET.ParseError):
        ET.fromstring(f"<svg>{plain}</g></svg>")


# --------------------------------------------------------- the inspection

def test_inspect_reports_a_colour_or_nothing(home, tmp_path):
    tables = dict(GOOD)
    tables["routes.txt"] = (
        "route_id,agency_id,route_short_name,route_long_name,route_type,"
        "route_color,route_text_color\n"
        'R1,M,1,Linea 1,1,"\'><svg onload=alert(1)>",0072bc\n'
        "R2,M,2,Linea 2,1,#AABBCC,red\n"
        "R3,M,3,Linea 3,1,ddeeff,\n"
    )
    tables["trips.txt"] = "route_id,service_id,trip_id\nR1,s,T1\nR2,s,T2\nR3,s,T3\n"
    feeds.add(gtfs_zip(tmp_path / "f.zip", tables), key="hostile")
    routes = feeds.inspect("hostile").to_dict()["routes"]
    assert [(r["color"], r["text_color"]) for r in routes] == [
        (None, "0072BC"),           # markup is not a colour; the other is kept
        ("AABBCC", None),           # the hash is dropped, a name is refused
        ("DDEEFF", None),
    ]
