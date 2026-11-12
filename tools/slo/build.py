"""Generate every SLO artefact, or report which committed ones are stale."""

from __future__ import annotations

import json
from pathlib import Path

from libs.core.cmdb import load_cmdb
from services.kit.metrics import LATENCY_BUCKETS
from tools import paths
from tools.slo.cmdb_rules import render_cmdb_rules
from tools.slo.docs import render_slo_design
from tools.slo.generator import render_rule_file
from tools.slo.models import SloSpec, load_specs

CMDB_RULES_FILE = "cmdb.yml"


def render_all(
    specs_dir: Path = paths.SLO_DIR, cmdb_file: Path = paths.CMDB_FILE
) -> dict[Path, str]:
    """Map every generated file path to its expected content."""
    specs = load_specs(specs_dir)
    outputs = {
        paths.GENERATED_RULES_DIR / f"{spec.name}.yml": render_rule_file(spec, LATENCY_BUCKETS)
        for spec in specs
    }
    outputs[paths.GENERATED_RULES_DIR / CMDB_RULES_FILE] = render_cmdb_rules(load_cmdb(cmdb_file))
    outputs[paths.SLO_DESIGN_DOC] = render_slo_design(specs)
    outputs[paths.SLO_SCHEMA_FILE] = (
        json.dumps(SloSpec.model_json_schema(), indent=2, sort_keys=True) + "\n"
    )
    return outputs


def _orphans(outputs: dict[Path, str]) -> list[Path]:
    """Generated rule files whose spec no longer exists."""
    return [path for path in sorted(paths.GENERATED_RULES_DIR.glob("*.yml")) if path not in outputs]


def write_all() -> list[Path]:
    """Write generated files; return the paths that changed or were removed."""
    outputs = render_all()
    changed = []
    for path, text in outputs.items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            changed.append(path)
    for orphan in _orphans(outputs):
        orphan.unlink()
        changed.append(orphan)
    return changed


def stale() -> list[Path]:
    """Generated files that differ from what the specs produce."""
    outputs = render_all()
    out_of_date = [
        path
        for path, text in outputs.items()
        if not path.exists() or path.read_text(encoding="utf-8") != text
    ]
    return [*out_of_date, *_orphans(outputs)]
