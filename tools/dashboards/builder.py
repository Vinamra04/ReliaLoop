"""Small helpers that build Grafana dashboard JSON.

Why generate: four dashboards share the same panel boilerplate. Defining them
in Python keeps every panel to a title, a description, a unit and its queries,
and the generated JSON is committed so Grafana needs nothing but the files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DATASOURCE = {"type": "prometheus", "uid": "prometheus"}
GRID_WIDTH = 24
SCHEMA_VERSION = 39


@dataclass(frozen=True)
class Query:
    expr: str
    legend: str = ""
    instant: bool = False


@dataclass(frozen=True)
class Threshold:
    value: float | None
    color: str


@dataclass
class Layout:
    """Places panels left to right, wrapping rows on a 24-column grid."""

    x: int = 0
    y: int = 0
    row_height: int = 0
    next_id: int = 1
    panels: list[dict[str, Any]] = field(default_factory=list)

    def _place(self, width: int, height: int) -> dict[str, int]:
        if self.x + width > GRID_WIDTH:
            self.x = 0
            self.y += self.row_height
            self.row_height = 0
        position = {"x": self.x, "y": self.y, "w": width, "h": height}
        self.x += width
        self.row_height = max(self.row_height, height)
        return position

    def _add(self, panel: dict[str, Any], width: int, height: int) -> None:
        panel["id"] = self.next_id
        panel["gridPos"] = self._place(width, height)
        self.next_id += 1
        self.panels.append(panel)

    def row(self, title: str) -> None:
        """Start a titled row; following panels sit beneath it."""
        self.x = GRID_WIDTH  # force a wrap
        self._add({"type": "row", "title": title, "collapsed": False, "panels": []}, GRID_WIDTH, 1)

    def timeseries(
        self,
        title: str,
        description: str,
        queries: list[Query],
        *,
        unit: str = "short",
        width: int = 8,
        height: int = 8,
        minimum: float | None = None,
        maximum: float | None = None,
        thresholds: list[Threshold] | None = None,
        stack: bool = False,
    ) -> None:
        defaults: dict[str, Any] = {
            "unit": unit,
            "custom": {
                "lineWidth": 1,
                "fillOpacity": 20 if stack else 8,
                "showPoints": "never",
                "spanNulls": False,
                "stacking": {"mode": "normal" if stack else "none"},
                "thresholdsStyle": {"mode": "line" if thresholds else "off"},
            },
        }
        if minimum is not None:
            defaults["min"] = minimum
        if maximum is not None:
            defaults["max"] = maximum
        if thresholds:
            defaults["thresholds"] = _thresholds(thresholds)
        self._add(
            {
                "type": "timeseries",
                "title": title,
                "description": description,
                "datasource": DATASOURCE,
                "targets": _targets(queries),
                "fieldConfig": {"defaults": defaults, "overrides": []},
                "options": {
                    "legend": {"displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "desc"},
                },
            },
            width,
            height,
        )

    def stat(
        self,
        title: str,
        description: str,
        queries: list[Query],
        *,
        unit: str = "short",
        width: int = 4,
        height: int = 4,
        thresholds: list[Threshold] | None = None,
        text_mode: str = "auto",
        decimals: int | None = None,
    ) -> None:
        defaults: dict[str, Any] = {
            "unit": unit,
            "thresholds": _thresholds(thresholds or [Threshold(None, "text")]),
        }
        if decimals is not None:
            defaults["decimals"] = decimals
        self._add(
            {
                "type": "stat",
                "title": title,
                "description": description,
                "datasource": DATASOURCE,
                "targets": _targets(queries),
                "fieldConfig": {"defaults": defaults, "overrides": []},
                "options": {
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value",
                    "graphMode": "none",
                    "textMode": text_mode,
                },
            },
            width,
            height,
        )

    def table(self, title: str, description: str, queries: list[Query], *, width: int = 12) -> None:
        self._add(
            {
                "type": "table",
                "title": title,
                "description": description,
                "datasource": DATASOURCE,
                "targets": [{**target, "format": "table"} for target in _targets(queries)],
                "fieldConfig": {"defaults": {}, "overrides": []},
                "options": {"showHeader": True},
                "transformations": [
                    {
                        "id": "organize",
                        "options": {"excludeByName": {"Time": True, "__name__": True}},
                    }
                ],
            },
            width,
            8,
        )


def _thresholds(steps: list[Threshold]) -> dict[str, Any]:
    return {
        "mode": "absolute",
        "steps": [{"value": step.value, "color": step.color} for step in steps],
    }


def _targets(queries: list[Query]) -> list[dict[str, Any]]:
    return [
        {
            "datasource": DATASOURCE,
            "refId": chr(ord("A") + index),
            "expr": query.expr,
            "legendFormat": query.legend,
            "instant": query.instant,
            "range": not query.instant,
        }
        for index, query in enumerate(queries)
    ]


def custom_variable(name: str, label: str, values: list[str]) -> dict[str, Any]:
    """A fixed-choice dashboard variable."""
    return {
        "type": "custom",
        "name": name,
        "label": label,
        "query": ",".join(values),
        "current": {"text": values[0], "value": values[0]},
        "options": [
            {"text": value, "value": value, "selected": index == 0}
            for index, value in enumerate(values)
        ],
        "multi": False,
        "includeAll": False,
    }


def dashboard(
    uid: str,
    title: str,
    description: str,
    layout: Layout,
    *,
    variables: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Wrap panels in the dashboard envelope."""
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["relialoop"],
        "schemaVersion": SCHEMA_VERSION,
        "version": 1,
        "editable": False,
        "graphTooltip": 1,
        "refresh": "5s",
        "time": {"from": "now-15m", "to": "now"},
        "timezone": "browser",
        "templating": {"list": variables or []},
        "annotations": {"list": []},
        "panels": layout.panels,
    }
