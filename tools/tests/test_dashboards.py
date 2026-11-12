import json
from pathlib import Path

from tools import paths
from tools.dashboards import generate
from tools.dashboards.builder import GRID_WIDTH, Layout, Query


def test_committed_dashboards_are_up_to_date():
    assert generate.stale(paths.DASHBOARDS_DIR) == []


def test_every_panel_has_title_description_unit_and_valid_position():
    for name, text in generate.render_all().items():
        board = json.loads(text)
        ids = [panel["id"] for panel in board["panels"]]
        assert len(ids) == len(set(ids)), name
        for panel in board["panels"]:
            assert panel["title"], name
            position = panel["gridPos"]
            assert position["x"] + position["w"] <= GRID_WIDTH
            if panel["type"] == "row":
                continue
            assert panel["description"], f"{name}: {panel['title']}"
            assert panel["targets"], f"{name}: {panel['title']}"
            if panel["type"] != "table":
                assert panel["fieldConfig"]["defaults"]["unit"]


def test_layout_wraps_and_rows_start_a_new_line():
    layout = Layout()
    for index in range(4):
        layout.timeseries(f"p{index}", "d", [Query("up")], width=8)
    layout.row("next")
    positions = [(p["gridPos"]["x"], p["gridPos"]["y"]) for p in layout.panels]
    assert positions == [(0, 0), (8, 0), (16, 0), (0, 8), (0, 16)]


def test_write_all_reports_only_changes(tmp_path: Path):
    first = generate.write_all(tmp_path)
    assert sorted(first) == sorted(generate.render_all())
    assert generate.write_all(tmp_path) == []
    assert generate.stale(tmp_path) == []
