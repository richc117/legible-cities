"""One Result, one set of numbers, three renderings that cannot drift (E05).

The synthetic tests build a Diagnostics by hand. The real ones read the
checkout's stored layouts for Los Angeles and Mexico City and hold the
summary text and the protocol's block byte-identical to the snapshots
taken before this module existed; they skip without the layouts.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from schematic import diagnostics, pipeline, site
from schematic.diagnostics import Diagnostics, StopMatching, caveats, issue_score

SNAPSHOTS = Path(__file__).parent / "fixtures" / "diagnostics"


def clean() -> Diagnostics:
    return Diagnostics(
        key="x", name="X", date=dt.date(2026, 9, 2), stations=10, junctions=1, edges=12,
        lines=("A", "B"), octilinear=0.998,
        stops=StopMatching(matched=12, total=12, by_station_id=10, by_parent_station=2,
                           by_name=0, unmatched=()),
        trips_total=100, paths=4, unrouted=0, skipped_calls=0, borrowed_track=0,
        labels_dropped=0, peak_concurrent=7,
    )


def test_a_clean_network_has_no_caveats_and_scores_zero():
    d = clean()
    assert caveats(d) == []
    assert issue_score(d) == 0.0


def test_to_dict_is_the_protocols_block_and_round_trips_through_json():
    d = clean()
    block = d.to_dict()
    assert list(block) == ["stations", "junctions", "edges", "lines", "octilinear", "stops",
                           "trips", "degraded", "labels_dropped", "peak_concurrent"]
    assert json.loads(json.dumps(block)) == block
    assert block["stops"]["by"] == {"station_id": 10, "parent_station": 2, "name": 0}


def test_caveats_say_the_consequence_and_the_score_weighs_proportions():
    from dataclasses import replace
    d = replace(clean(), unrouted=5, skipped_calls=25, borrowed_track=50, labels_dropped=2,
                unmatched_count=3, stops=replace(clean().stops, matched=9, unmatched=("s1", "s2", "s3")))
    said = caveats(d)
    assert said[0] == "3 of 12 stops could not be placed on the map, so trains pass straight through them"
    assert "5 trips could not be traced" in said[1]
    assert "25 trips (25%) skip a stop" in said[2]
    assert "50 trips (50%) run part of the way" in said[3]
    assert said[4] == ("2 station names had nowhere to sit without overlapping another and are "
                       "not drawn")
    expected = (3.0 * 3 / 12 + 3.0 * 5 / 100 + 2.0 * 25 / 100 + 1.0 * 50 / 100 + 1.0 * 2 / 10)
    assert issue_score(d) == pytest.approx(expected)
    # A single dropped label reads in the singular.
    one = replace(clean(), labels_dropped=1)
    assert caveats(one) == ["1 station name had nowhere to sit without overlapping another and is not drawn"]


def test_the_site_reads_the_same_weights():
    assert site.ISSUE_WEIGHTS is diagnostics.ISSUE_WEIGHTS


@pytest.mark.parametrize("key,date", [("la-metro-rail", dt.date(2026, 9, 2)),
                                      ("cdmx-metro", dt.date(2025, 7, 1))])
def test_summary_and_block_are_byte_identical_to_before(key, date, tmp_path):
    stored = pipeline.stored(key)
    summary_file = SNAPSHOTS / f"{key}.summary.txt"
    if stored is None or not summary_file.exists():
        pytest.skip(f"needs the stored layout and the snapshot for {key}")
    result = pipeline.run(key, layout=stored.id, date=date, out_dir=tmp_path)
    diag = result.diagnostics()
    assert result.summary() + "\n" == summary_file.read_text()
    assert diag.to_dict() == json.loads((SNAPSHOTS / f"{key}.diagnostics.json").read_text())
    # The site's two renderings are this module's.
    assert site._caveats(result) == caveats(diag)
    assert site._issue_score(result) == issue_score(diag)
