"""Feeds decorate stop names heavily; both the map and the matcher depend on
stripping exactly the same things."""

import pytest

from schematic.names import display_name, normalize_name


@pytest.mark.parametrize("raw,expected", [
    ("Downtown Long Beach Station", "Downtown Long Beach"),
    ("Union Station - Metro B & D Lines", "Union Station"),
    ("7th Street / Metro Center Station - Metro A & E Lines", "7th Street / Metro Center"),
    ("Willowbrook - Rosa Parks Station - Metro A-Line", "Willowbrook - Rosa Parks"),
    ("Expo / Crenshaw K-Line Station", "Expo / Crenshaw"),
])
def test_display_name(raw, expected):
    assert display_name(raw) == expected


def test_display_name_keeps_a_load_bearing_suffix():
    """"Union" is not what anyone calls Union Station."""
    assert display_name("Union Station") == "Union Station"
    assert display_name("Vernon Station") == "Vernon Station"


def test_normalize_keeps_metro_as_a_place_word():
    """Stripping "metro" as noise would wreck 7th Street/Metro Center."""
    assert normalize_name("7th Street/Metro Center Station") == "7th street metro center"


def test_line_designators_do_not_split_a_shared_station():
    """The two Expo/Crenshaw platforms must fold onto one name."""
    assert (normalize_name("Expo / Crenshaw K-Line Station")
            == normalize_name("Expo / Crenshaw E-Line Station"))
