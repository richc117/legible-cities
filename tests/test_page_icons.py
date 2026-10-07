"""The view switcher's four icons: which files, whose, and that nothing turns them.

The icons are Phosphor's regular weight, copied unmodified from the
``@phosphor-icons/core`` 2.1.1 release (MIT; the licence text ships beside
them). ``animate.py`` base64s each file into the page and the site masks the
same files from CSS, so the things that can drift without anyone seeing it in
a browser are the ones asserted here: that the table and the folder agree,
that each file is a Phosphor glyph, that the page carries the file's own bytes,
and that no CSS rule turns a glyph on its side to make it say something else
(the Calcite icon these replaced needed a 90 degree turn; ``graph`` does not).
"""

import base64
import re
from pathlib import Path

from schematic import animate, site

ICON_DIR = Path(animate.__file__).parent / "page" / "icons"
PAGE = ICON_DIR.parent / "page.html"
SITE_STYLE = site.ASSETS_DIR / "style.css"

GLYPHS = {"map-trifold", "graph", "line-segments", "clock"}


def _files() -> set[str]:
    return {p.stem for p in ICON_DIR.glob("*.svg")}


def test_the_icon_table_is_the_files_on_disk():
    """A key with no file fails at import; a file with no key ships dead weight."""
    assert set(animate._VIEW_ICONS) == _files()


def test_the_four_glyphs_are_the_ones_chosen():
    assert set(animate._VIEW_ICONS) == GLYPHS


def test_every_icon_is_drawn_on_phosphors_256_unit_grid():
    for name in animate._VIEW_ICONS:
        svg = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")
        assert 'viewBox="0 0 256 256"' in svg, name


def test_the_page_carries_each_file_byte_for_byte():
    """The data URI decodes to the file on disk: nothing was redrawn on the way."""
    prefix = "data:image/svg+xml;base64,"
    for name, uri in animate._VIEW_ICONS.items():
        assert uri.startswith(prefix), name
        assert base64.b64decode(uri[len(prefix):]) == (ICON_DIR / f"{name}.svg").read_bytes(), name


def test_the_licence_ships_beside_the_icons():
    text = (ICON_DIR / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Phosphor Icons" in text


def test_the_page_template_names_each_button_and_turns_none():
    page = PAGE.read_text(encoding="utf-8")
    for placeholder in ("__ICON_GEO__", "__ICON_SCHEMATIC__", "__ICON_LINEAR__",
                        "__ICON_TIME__"):
        assert placeholder in page, placeholder
    # The CSS turn only: the label placer's own SVG ``rotate(`` is another thing.
    assert "rotate(90deg)" not in page


def test_the_site_masks_the_same_files_and_turns_none():
    css = SITE_STYLE.read_text(encoding="utf-8")
    named = re.findall(r'--icon:\s*url\("icons/([^"]+)\.svg"\)', css)
    assert sorted(named) == sorted(GLYPHS)
    assert "rotate(90deg)" not in css
