"""schematic.pipeline.stages_for: a layout's tuning turned into LOOM's flags.

Pure, so no LOOM and no home: the table of flags, LOOM's defaults and the
stage each flag goes to, and the rules that keep a layout's id stable (a
default writes nothing, equal numbers are written alike, the order is the
table's). What the flags do to the stored layouts is in test_layouts, and
what the server refuses is in test_serve.
"""

from __future__ import annotations

import pytest

from schematic import pipeline
from schematic.pipeline import stages_for

# Every field at a value that is not LOOM's own default.
FULL = {"merge_distance": 75, "grid": "hexalinear", "grid_size": 150,
        "penalties": {"deg45": 3, "deg90": 2.5, "deg135": 1.25, "deg180": 0.5, "diagonal": 1}}

# The flags that tuning writes, as the tools get them, in the fixed order.
FULL_TOPO = ("-d", "75")
FULL_OCTI = ("-b", "hexalinear", "-g", "150%", "--pen-45", "3", "--pen-90", "2.5",
             "--pen-135", "1.25", "--pen-180", "0.5", "--diag-pen", "1")


def stage(stages, tool: str) -> tuple[str, ...]:
    [args] = [args for name, args in stages if name == tool]
    return args


def test_no_tuning_and_a_tuning_of_defaults_are_the_stages_untouched():
    defaults = {"merge_distance": 50, "grid": "octilinear", "grid_size": 100,
                "penalties": {"deg45": 2, "deg90": 1.5, "deg135": 1, "deg180": 0,
                              "diagonal": 0.5}}
    for tuning in (None, {}, {"penalties": {}}, defaults, {"merge_distance": 50.0},
                   {"grid_size": 100.0}, {"penalties": {"deg180": 0.0, "deg135": 1.0}}):
        assert stages_for(tuning) == pipeline.STAGES, tuning
    assert stages_for() == pipeline.STAGES


def test_a_full_tuning_goes_to_topo_and_octi_in_a_fixed_order_and_loom_gets_none():
    stages = stages_for(FULL)
    assert [tool for tool, _args in stages] == ["topo", "loom", "octi"]
    assert stage(stages, "topo") == FULL_TOPO
    assert stage(stages, "loom") == ()
    assert stage(stages, "octi") == FULL_OCTI


def test_the_order_of_the_flags_is_the_tables_not_the_clients():
    shuffled = {"penalties": dict(reversed(list(FULL["penalties"].items()))),
                "grid_size": 150, "grid": "hexalinear", "merge_distance": 75}
    assert list(shuffled) != list(FULL)
    assert stages_for(shuffled) == stages_for(FULL)


@pytest.mark.parametrize("tuning,tool,flags", [
    ({"merge_distance": 75}, "topo", ("-d", "75")),
    ({"grid": "ortholinear"}, "octi", ("-b", "ortholinear")),
    ({"grid": "orthoradial"}, "octi", ("-b", "orthoradial")),
    ({"grid": "hexalinear"}, "octi", ("-b", "hexalinear")),
    ({"grid_size": 150}, "octi", ("-g", "150%")),
    ({"penalties": {"deg45": 3}}, "octi", ("--pen-45", "3")),
    ({"penalties": {"deg90": 2.5}}, "octi", ("--pen-90", "2.5")),
    ({"penalties": {"deg135": 1.25}}, "octi", ("--pen-135", "1.25")),
    ({"penalties": {"deg180": 0.5}}, "octi", ("--pen-180", "0.5")),
    ({"penalties": {"diagonal": 1}}, "octi", ("--diag-pen", "1")),
])
def test_each_field_writes_its_own_flag_on_its_own_stage_and_nothing_else(tuning, tool, flags):
    stages = stages_for(tuning)
    assert stage(stages, tool) == flags
    assert [(name, args) for name, args in stages if name != tool] == [
        (name, args) for name, args in pipeline.STAGES if name != tool]


def test_the_bounds_themselves_are_flags_like_any_other_value():
    low = stages_for({"merge_distance": 5, "grid_size": 25,
                      "penalties": {"deg45": 0, "deg90": 0, "deg135": 0, "diagonal": 0}})
    assert stage(low, "topo") == ("-d", "5")
    assert stage(low, "octi") == ("-g", "25%", "--pen-45", "0", "--pen-90", "0",
                                  "--pen-135", "0", "--diag-pen", "0")
    high = stages_for({"merge_distance": 500, "grid_size": 400,
                       "penalties": {"deg45": 10, "deg180": 10}})
    assert stage(high, "topo") == ("-d", "500")
    assert stage(high, "octi") == ("-g", "400%", "--pen-45", "10", "--pen-180", "10")


def test_a_number_is_written_as_loom_prints_it_whether_it_came_as_an_integer_or_a_float():
    for sent in (75, 75.0):
        assert stage(stages_for({"merge_distance": sent}), "topo") == ("-d", "75")
    for sent in (150, 150.0):
        assert stage(stages_for({"grid_size": sent}), "octi") == ("-g", "150%")
    for sent in (3, 3.0):
        assert stage(stages_for({"penalties": {"deg45": sent}}), "octi") == ("--pen-45", "3")
    assert stage(stages_for({"penalties": {"deg90": 2.5}}), "octi") == ("--pen-90", "2.5")
    assert stage(stages_for({"penalties": {"diagonal": 0.75}}), "octi") == ("--diag-pen", "0.75")
    assert [pipeline._written(v) for v in (50.0, 50, 1.5, 0.5, 0.25, 0.0, -0.0, 12.75)] == [
        "50", "50", "1.5", "0.5", "0.25", "0", "0", "12.75"]
    for tuning in ({"merge_distance": 75.0}, {"grid_size": 150.0},
                   {"penalties": {"deg45": 3.0, "diagonal": 0.75}}):
        for _tool, args in stages_for(tuning):
            assert not any(arg.endswith(".0") or arg.endswith(".0%") for arg in args), args


def test_a_default_beside_a_value_that_is_not_writes_only_the_one():
    stages = stages_for({"merge_distance": 50, "grid": "octilinear", "grid_size": 200,
                         "penalties": {"deg45": 2, "deg90": 1.5, "deg135": 0.5}})
    assert stage(stages, "topo") == ()
    assert stage(stages, "octi") == ("-g", "200%", "--pen-135", "0.5")


def test_building_the_stages_never_changes_the_defaults_they_are_built_from():
    before = [(tool, tuple(args)) for tool, args in pipeline.STAGES]
    stages_for(FULL)
    stages_for(None)
    assert pipeline.STAGES == before == [("topo", ()), ("loom", ()), ("octi", ())]
    assert stages_for(FULL) is not pipeline.STAGES


def test_the_table_holds_loom_defaults_and_the_ranges_the_issue_settled():
    assert (pipeline.MERGE_DISTANCE.flag, pipeline.MERGE_DISTANCE.default,
            pipeline.MERGE_DISTANCE.low, pipeline.MERGE_DISTANCE.high) == ("-d", 50, 5, 500)
    assert (pipeline.GRID_SIZE.flag, pipeline.GRID_SIZE.default,
            pipeline.GRID_SIZE.low, pipeline.GRID_SIZE.high) == ("-g", 100, 25, 400)
    assert (pipeline.GRID_FLAG, pipeline.GRID_DEFAULT) == ("-b", "octilinear")
    assert pipeline.GRIDS == ("octilinear", "ortholinear", "orthoradial", "hexalinear")
    assert {name: (t.flag, t.default, t.low, t.high) for name, t in pipeline.PENALTIES.items()} == {
        "deg45": ("--pen-45", 2, 0, 10), "deg90": ("--pen-90", 1.5, 0, 10),
        "deg135": ("--pen-135", 1, 0, 10), "deg180": ("--pen-180", 0, 0, 10),
        "diagonal": ("--diag-pen", 0.5, 0, 10)}
