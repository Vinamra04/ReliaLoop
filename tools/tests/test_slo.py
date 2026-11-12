from pathlib import Path

import pytest
import yaml

from libs.core import naming
from services.kit.metrics import LATENCY_BUCKETS
from tools import paths
from tools.slo import build, generator
from tools.slo.docs import render_slo_design
from tools.slo.models import SloSpec, SpecError, load_spec, load_specs
from tools.slo.profiles import DEMO, PRODUCTION, duration_seconds

AVAILABILITY = {
    "name": "gateway-availability",
    "service": "gateway",
    "description": "Non-5xx share of gateway API requests",
    "sli": {
        "type": "availability",
        "total": 'http_requests_total{route!~"/healthz"}',
        "errors": 'http_requests_total{status=~"5..", route!~"/healthz"}',
    },
    "objective": 99.5,
    "window": "30d",
    "alerting": {
        "profile": "demo",
        "min_traffic_rps": 1,
        "runbook": "gateway-availability",
        "owner": "platform-team",
    },
}
LATENCY = {
    **AVAILABILITY,
    "name": "gateway-latency",
    "sli": {
        "type": "latency",
        "histogram": "http_request_duration_seconds",
        "threshold_seconds": 0.5,
    },
    "objective": 95,
}


def spec(data: dict, **overrides) -> SloSpec:
    return SloSpec.model_validate({**data, **overrides})


def rules_by_name(groups: dict) -> dict[str, dict]:
    return {rule.get("alert") or rule["record"]: rule for rule in groups["groups"][0]["rules"]}


# --- spec validation ---------------------------------------------------------


def test_repository_specs_load_and_are_the_four_required_slos():
    specs = load_specs(paths.SLO_DIR)
    assert [s.name for s in specs] == [
        "gateway-availability",
        "gateway-latency",
        "orders-availability",
        "orders-latency",
    ]
    objectives = {s.name: s.objective for s in specs}
    assert objectives == {
        "gateway-availability": 99.5,
        "gateway-latency": 95,
        "orders-availability": 99.5,
        "orders-latency": 95,
    }


def test_error_budget():
    assert spec(AVAILABILITY).error_budget == pytest.approx(0.005)
    assert spec(LATENCY).error_budget == pytest.approx(0.05)


@pytest.mark.parametrize(
    "bad",
    [
        {"objective": 100},
        {"objective": 40},
        {"name": "Bad Name"},
        {"window": "thirty days"},
        {"unknown_field": 1},
        {"sli": {"type": "availability", "total": "sum(rate(x[5m]))", "errors": "x"}},
        {"sli": {"type": "throughput", "total": "x", "errors": "x"}},
        {"alerting": {**AVAILABILITY["alerting"], "profile": "staging"}},
    ],
)
def test_invalid_specs_are_rejected(bad: dict):
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic's ValidationError subclass
        spec(AVAILABILITY, **bad)


def test_load_spec_reports_the_file_name(tmp_path: Path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("name: x\n", encoding="utf-8")
    with pytest.raises(SpecError, match=r"broken\.yaml"):
        load_spec(broken)


def test_duplicate_names_and_empty_directories_are_rejected(tmp_path: Path):
    with pytest.raises(SpecError, match="no SLO specs"):
        load_specs(tmp_path)
    for filename in ("a.yaml", "b.yaml"):
        (tmp_path / filename).write_text(yaml.safe_dump(AVAILABILITY), encoding="utf-8")
    with pytest.raises(SpecError, match="duplicate SLO names: gateway-availability"):
        load_specs(tmp_path)


# --- profiles ----------------------------------------------------------------


def test_production_profile_is_exactly_the_sre_workbook_table():
    table = [
        (t.tier, t.factor, t.long_window, t.short_window, t.severity) for t in PRODUCTION.tiers
    ]
    assert table == [
        ("fast", 14.4, "1h", "5m", "page"),
        ("slow", 6, "6h", "30m", "page"),
        ("ticket", 3, "1d", "2h", "ticket"),
        ("trickle", 1, "3d", "6h", "ticket"),
    ]
    assert PRODUCTION.budget_window is None


def test_workbook_budget_consumption_for_a_30_day_window():
    """14.4x for 1h spends 2% of a 30-day budget; 6x for 6h spends 5%; 3x for 1d spends 10%."""
    month = duration_seconds("30d")
    spent = {
        t.tier: round(t.factor * duration_seconds(t.long_window) / month * 100, 6)
        for t in PRODUCTION.tiers
    }
    assert spent == {"fast": 2.0, "slow": 5.0, "ticket": 10.0, "trickle": 10.0}


def test_short_window_is_one_twelfth_of_the_long_window_in_production():
    for tier in PRODUCTION.tiers:
        assert duration_seconds(tier.long_window) == 12 * duration_seconds(tier.short_window)


def test_demo_profile_keeps_the_factors_and_compresses_the_windows():
    assert [t.factor for t in DEMO.tiers] == [t.factor for t in PRODUCTION.tiers]
    assert [(t.long_window, t.short_window) for t in DEMO.tiers] == [
        ("2m", "30s"),
        ("6m", "1m"),
        ("15m", "3m"),
        ("30m", "5m"),
    ]
    assert DEMO.budget_window == "1h"
    assert DEMO.windows == ("30s", "1m", "2m", "3m", "5m", "6m", "15m", "30m")
    for demo, production in zip(DEMO.tiers, PRODUCTION.tiers, strict=True):
        assert duration_seconds(demo.long_window) < duration_seconds(production.long_window)
        assert duration_seconds(demo.for_duration) <= 15


# --- generator ---------------------------------------------------------------


def test_with_matchers():
    assert generator.with_matchers("m", ['service="a"']) == 'm{service="a"}'
    assert (
        generator.with_matchers('m{x="1"}', ['service="a"', 'le="0.5"'], "_bucket")
        == 'm_bucket{service="a", le="0.5", x="1"}'
    )
    with pytest.raises(SpecError):
        generator.with_matchers("sum(m)", [])


def test_bucket_label_matches_prometheus_client_rendering():
    assert generator.bucket_label(0.5) == "0.5"
    assert generator.bucket_label(1) == "1.0"
    assert generator.bucket_label(0.025) == "0.025"


def test_availability_expression_handles_zero_errors():
    expr = generator.error_ratio_expr(spec(AVAILABILITY), "5m", LATENCY_BUCKETS)
    assert expr == (
        '(sum(rate(http_requests_total{service="gateway", status=~"5..", '
        'route!~"/healthz"}[5m])) or vector(0)) / '
        'sum(rate(http_requests_total{service="gateway", route!~"/healthz"}[5m]))'
    )


def test_latency_expression_uses_the_threshold_bucket():
    expr = generator.error_ratio_expr(spec(LATENCY), "30s", LATENCY_BUCKETS)
    assert expr == (
        '1 - (sum(rate(http_request_duration_seconds_bucket{service="gateway", le="0.5"}[30s])) '
        '/ sum(rate(http_request_duration_seconds_count{service="gateway"}[30s])))'
    )


def test_latency_threshold_must_be_an_existing_bucket():
    bad = spec(LATENCY, sli={**LATENCY["sli"], "threshold_seconds": 0.4})
    with pytest.raises(SpecError, match=r"0.4s is not a histogram bucket"):
        generator.build_rule_groups(bad, LATENCY_BUCKETS)


def test_slo_thresholds_are_real_buckets():
    assert 0.3 in LATENCY_BUCKETS
    assert 0.5 in LATENCY_BUCKETS


def test_production_rules_use_workbook_factors_and_windows():
    rules = rules_by_name(
        generator.build_rule_groups(spec(AVAILABILITY), LATENCY_BUCKETS, "production")
    )
    expected = {
        "SLOBurnRateFast": ("14.4", "1h", "5m", "page"),
        "SLOBurnRateSlow": ("6", "6h", "30m", "page"),
        "SLOBurnRateTicket": ("3", "1d", "2h", "ticket"),
        "SLOBurnRateTrickle": ("1", "3d", "6h", "ticket"),
    }
    for alert, (factor, long_window, short_window, severity) in expected.items():
        rule = rules[alert]
        assert rule["expr"] == (
            f'slo:burn_rate:ratio_{long_window}{{slo="gateway-availability"}} > {factor}\n'
            f'and slo:burn_rate:ratio_{short_window}{{slo="gateway-availability"}} > {factor}\n'
            f'and slo:sli_total:rate{long_window}{{slo="gateway-availability"}} > 1'
        )
        assert rule["labels"]["severity"] == severity
        assert rule["labels"]["class"] == "symptom"
        assert rule["labels"]["slo"] == "gateway-availability"
        assert rule["labels"]["service"] == "gateway"
        assert set(rule["annotations"]) == {
            "summary",
            "description",
            "runbook_url",
            "dashboard_url",
        }
    # The budget is accounted over the spec's own 30-day window.
    assert rules[naming.ERROR_BUDGET_REMAINING]["expr"] == (
        '1 - slo:burn_rate:ratio_30d{slo="gateway-availability"}'
    )
    assert "slo:sli_error:ratio_rate30d" in rules


def test_demo_rules_account_the_budget_over_one_hour():
    rules = rules_by_name(generator.build_rule_groups(spec(AVAILABILITY), LATENCY_BUCKETS))
    assert rules[naming.ERROR_BUDGET_REMAINING]["expr"] == (
        '1 - slo:burn_rate:ratio_1h{slo="gateway-availability"}'
    )
    assert rules["slo:burn_rate:ratio_2m"]["expr"] == (
        'slo:sli_error:ratio_rate2m{slo="gateway-availability"} / 0.005'
    )
    assert rules[naming.OBJECTIVE]["expr"] == "vector(0.995)"
    assert rules["SLOBurnRateFast"]["for"] == "0s"


def test_recording_rules_precede_the_alerts_that_use_them():
    names = [
        rule.get("alert") or rule["record"]
        for rule in generator.build_rule_groups(spec(LATENCY), LATENCY_BUCKETS)["groups"][0][
            "rules"
        ]
    ]
    first_alert = min(i for i, name in enumerate(names) if name.startswith("SLOBurnRate"))
    assert all(not name.startswith("SLOBurnRate") for name in names[:first_alert])
    assert names.index("slo:sli_error:ratio_rate2m") < names.index("slo:burn_rate:ratio_2m")


def test_rendered_file_is_valid_yaml_with_header_and_no_anchors():
    text = generator.render_rule_file(spec(AVAILABILITY), LATENCY_BUCKETS)
    assert text.startswith("# GENERATED")
    assert "&id" not in text
    assert yaml.safe_load(text)["groups"][0]["name"] == "slo-gateway-availability"


# --- build / docs ------------------------------------------------------------


def test_committed_generated_files_are_up_to_date():
    assert build.stale() == []


def test_render_all_covers_rules_cmdb_docs_and_schema():
    names = {path.name for path in build.render_all()}
    assert names == {
        "gateway-availability.yml",
        "gateway-latency.yml",
        "orders-availability.yml",
        "orders-latency.yml",
        "cmdb.yml",
        "slo-design.md",
        "schema.json",
    }


def test_slo_design_doc_lists_every_slo_and_both_profiles():
    specs = load_specs(paths.SLO_DIR)
    text = render_slo_design(specs)
    for item in specs:
        assert f"`{item.name}`" in text
    assert "| `SLOBurnRateFast` | 14.4 | 1h | 5m | 0s | page | 2% |" in text
    assert "| `SLOBurnRateFast` | 14.4 | 2m | 30s | 0s | page | 48% |" in text
    assert "| `gateway-availability` | 7.2% | 3% | 1.5% | 0.5% |" in text
