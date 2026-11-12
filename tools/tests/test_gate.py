import httpx
import pytest
import respx

from libs.core.gate import EXIT_ALLOW, EXIT_BLOCKED, EXIT_ERROR, decide
from tools.gate.check import check_gate, fetch_budgets

PROM = "http://prom:9090"
QUERY = f"{PROM}/api/v1/query"
SLOS = ["orders-availability", "orders-latency"]


def result(**budgets: str) -> dict:
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"slo": slo.replace("_", "-"), "service": "orders"}, "value": [1, value]}
                for slo, value in budgets.items()
            ]
        },
    }


# --- the pure decision ----------------------------------------------------------------


def test_healthy_budgets_allow():
    decision = decide({"a": 0.9, "b": 0.25}, min_budget=0.25)
    assert (decision.allowed, decision.exit_code) == (True, EXIT_ALLOW)
    assert not decision.overridden
    assert not decision.failed_open


@pytest.mark.parametrize("value", [0.249, 0.0, -3.5])
def test_low_budget_blocks_with_exit_2(value: float):
    decision = decide({"a": 0.9, "b": value}, min_budget=0.25)
    assert (decision.allowed, decision.exit_code) == (False, EXIT_BLOCKED)
    assert "b has" in decision.reason


def test_the_worst_slo_is_named():
    decision = decide({"a": 0.1, "b": -2.0, "c": 0.2}, min_budget=0.25)
    assert "b has -200%" in decision.reason


def test_override_lets_a_low_budget_through_and_is_recorded():
    decision = decide({"a": 0.05}, min_budget=0.25, override_reason="hotfix for INC-1")
    assert (decision.allowed, decision.exit_code, decision.overridden) == (True, EXIT_ALLOW, True)
    assert "hotfix for INC-1" in decision.reason
    assert decision.reason.startswith("OVERRIDE")


def test_missing_budget_fails_closed_with_exit_3():
    decision = decide({"a": 0.9, "b": None}, min_budget=0.25)
    assert (decision.allowed, decision.exit_code) == (False, EXIT_ERROR)
    assert "no budget data for b" in decision.reason


def test_unreachable_prometheus_fails_closed_even_with_an_override():
    decision = decide({}, min_budget=0.25, error="Prometheus is unreachable", override_reason="x")
    assert (decision.allowed, decision.exit_code) == (False, EXIT_ERROR)
    assert "fail closed" in decision.reason


def test_fail_open_allows_unknown_state_loudly():
    for kwargs in ({"error": "Prometheus is unreachable"}, {}):
        budgets = {} if kwargs else {"a": None}
        decision = decide(budgets, min_budget=0.25, fail_open=True, **kwargs)
        assert (decision.allowed, decision.failed_open) == (True, True)
        assert decision.reason.startswith("FAIL-OPEN")


def test_fail_open_does_not_bypass_a_known_low_budget():
    decision = decide({"a": 0.1}, min_budget=0.25, fail_open=True)
    assert (decision.allowed, decision.exit_code) == (False, EXIT_BLOCKED)


def test_service_without_slos_fails_closed():
    decision = decide({}, min_budget=0.25)
    assert (decision.allowed, decision.exit_code) == (False, EXIT_ERROR)
    assert "no SLOs" in decision.reason


# --- against Prometheus fixtures --------------------------------------------------------


@respx.mock
def test_budget_high_allows(respx_mock: respx.MockRouter):
    route = respx_mock.get(QUERY).respond(
        json=result(orders_availability="0.8", orders_latency="1")
    )
    with httpx.Client() as client:
        decision = check_gate(client, PROM, "orders", SLOS)
    assert decision.allowed is True
    assert decision.budgets == {"orders-availability": 0.8, "orders-latency": 1.0}
    assert route.calls[0].request.url.params["query"] == (
        'slo:error_budget_remaining:ratio{service="orders"}'
    )


@respx.mock
def test_budget_low_blocks_and_override_allows(respx_mock: respx.MockRouter):
    respx_mock.get(QUERY).respond(json=result(orders_availability="-4.2", orders_latency="0.9"))
    with httpx.Client() as client:
        blocked = check_gate(client, PROM, "orders", SLOS)
        overridden = check_gate(client, PROM, "orders", SLOS, override_reason="demo")
    assert (blocked.allowed, blocked.exit_code) == (False, EXIT_BLOCKED)
    assert "orders-availability" in blocked.reason
    assert (overridden.allowed, overridden.overridden) == (True, True)


@respx.mock
def test_budget_missing_or_nan_fails_closed(respx_mock: respx.MockRouter):
    with httpx.Client() as client:
        respx_mock.get(QUERY).respond(json=result(orders_availability="0.8"))
        missing = check_gate(client, PROM, "orders", SLOS)
        respx_mock.get(QUERY).respond(json=result(orders_availability="0.8", orders_latency="NaN"))
        nan = check_gate(client, PROM, "orders", SLOS)
    for decision in (missing, nan):
        assert (decision.allowed, decision.exit_code) == (False, EXIT_ERROR)
        assert "orders-latency" in decision.reason


@respx.mock
def test_prometheus_unreachable_fails_closed_unless_fail_open(respx_mock: respx.MockRouter):
    respx_mock.get(QUERY).mock(side_effect=httpx.ConnectError("refused"))
    with httpx.Client() as client:
        closed = check_gate(client, PROM, "orders", SLOS)
        opened = check_gate(client, PROM, "orders", SLOS, fail_open=True)
    assert (closed.allowed, closed.exit_code) == (False, EXIT_ERROR)
    assert "unreachable (ConnectError)" in closed.reason
    assert (opened.allowed, opened.failed_open) == (True, True)


@respx.mock
def test_prometheus_errors_fail_closed(respx_mock: respx.MockRouter):
    with httpx.Client() as client:
        respx_mock.get(QUERY).respond(500)
        assert check_gate(client, PROM, "orders", SLOS).exit_code == EXIT_ERROR
        respx_mock.get(QUERY).respond(200, json={"status": "error", "error": "bad"})
        assert check_gate(client, PROM, "orders", SLOS).exit_code == EXIT_ERROR
        with pytest.raises(ValueError, match="bad"):
            fetch_budgets(client, PROM, "orders", SLOS)


@respx.mock
def test_min_budget_is_configurable(respx_mock: respx.MockRouter):
    respx_mock.get(QUERY).respond(json=result(orders_availability="0.4", orders_latency="0.4"))
    with httpx.Client() as client:
        assert check_gate(client, PROM, "orders", SLOS, min_budget=0.5).allowed is False
        assert check_gate(client, PROM, "orders", SLOS, min_budget=0.3).allowed is True
