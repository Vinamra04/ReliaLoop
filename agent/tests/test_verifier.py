from agent.app.domain import Category, Incident
from agent.ports.protocols import Sample
from agent.tests.fakes import T0, FakeExecutor, FakeMetrics, cmdb, container, settings
from agent.verify.verifier import Verifier

BURN = 'slo:burn_rate:ratio_30s{slo="%s"}'


def incident(service: str = "gateway", slo: str | None = "gateway-availability") -> Incident:
    return Incident(id="INC-1", service=service, slo=slo, opened_at=T0, updated_at=T0)


class Rig:
    def __init__(self, **overrides) -> None:
        self.metrics = FakeMetrics()
        self.executor = FakeExecutor()
        self.sleeps: list[float] = []
        self.on_sleep = None
        self.settings = settings(**overrides)
        self.verifier = Verifier(
            settings=self.settings,
            cmdb=cmdb(),
            catalog=self.metrics.catalog,
            metrics=self.metrics,
            executor=self.executor,
            sleep=self._sleep,
        )
        for slo in ("gateway-availability", "gateway-latency", "orders-availability"):
            self.burn(slo, 0.0)

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))

    def burn(self, slo: str, value: float | None) -> None:
        self.metrics.raw[BURN % slo] = [] if value is None else [Sample(labels={}, value=value)]


async def test_recovered_after_three_consecutive_healthy_checks():
    rig = Rig(verify_wait_s=20, verify_interval_s=10)
    result = await rig.verifier.verify(incident(), "orders", Category.ERROR_SPIKE)
    assert result.recovered is True
    assert result.checks == 3
    assert rig.sleeps == [20, 10, 10]  # initial wait, then the poll interval


async def test_burn_rate_must_be_below_half_the_fast_burn_factor():
    rig = Rig()
    rig.burn("gateway-availability", 7.2)
    assert (await rig.verifier.verify(incident(), "orders", None)).recovered is False
    rig.burn("gateway-availability", 7.1)
    assert (await rig.verifier.verify(incident(), "orders", None)).recovered is True


async def test_a_failed_check_resets_the_consecutive_counter():
    rig = Rig(verify_timeout_s=5)

    def flap(sleep_number: int) -> None:
        # healthy, healthy, burning again, then healthy from there on
        rig.burn("gateway-availability", 50.0 if sleep_number == 3 else 0.0)

    rig.on_sleep = flap
    result = await rig.verifier.verify(incident(), "orders", None)
    assert result.recovered is True
    assert result.checks == 6


async def test_times_out_with_the_last_problem_when_still_burning():
    rig = Rig()
    rig.burn("gateway-availability", 100.0)
    result = await rig.verifier.verify(incident(), "orders", None)
    assert result.recovered is False
    assert "gateway-availability burn rate 100.0 is not below 7.2" in result.detail


async def test_missing_sli_data_is_not_recovery():
    rig = Rig()
    rig.burn("gateway-availability", None)
    result = await rig.verifier.verify(incident(), "orders", None)
    assert result.recovered is False
    assert "no SLI data" in result.detail


async def test_unhealthy_or_missing_containers_block_recovery():
    rig = Rig()
    rig.executor.containers["orders"] = [container(health="starting")]
    result = await rig.verifier.verify(incident(), "orders", None)
    assert result.recovered is False
    assert "relialoop-orders-1=running/starting" in result.detail

    rig.executor.containers["orders"] = []
    assert "has no containers" in (await rig.verifier.verify(incident(), "orders", None)).detail


async def test_incident_without_slo_checks_every_slo_of_its_service():
    rig = Rig()
    rig.burn("gateway-latency", 20.0)
    result = await rig.verifier.verify(incident(slo=None), "gateway", Category.MEMORY_LEAK)
    assert result.recovered is False
    assert "gateway-latency" in result.detail


async def test_crash_loop_needs_stable_restart_counts():
    rig = Rig(verify_timeout_s=0.02)
    counter = {"n": 0}

    def keep_crashing(_: int) -> None:
        counter["n"] += 1
        rig.executor.containers["orders"] = [container(restart_count=counter["n"])]

    rig.on_sleep = keep_crashing
    result = await rig.verifier.verify(incident(), "orders", Category.CRASH_LOOP)
    assert result.recovered is False
    assert "restarted again" in result.detail

    rig.on_sleep = None
    assert (await rig.verifier.verify(incident(), "orders", Category.CRASH_LOOP)).recovered is True


async def test_memory_leak_needs_memory_back_below_the_threshold():
    rig = Rig()
    rig.metrics.set("rss_pct_of_limit", "gateway", 0.85)
    result = await rig.verifier.verify(incident(slo=None), "gateway", Category.MEMORY_LEAK)
    assert result.recovered is False
    assert "memory still at 85%" in result.detail
    rig.metrics.set("rss_pct_of_limit", "gateway", 0.3)
    assert (
        await rig.verifier.verify(incident(slo=None), "gateway", Category.MEMORY_LEAK)
    ).recovered


async def test_unreachable_sources_count_as_not_recovered():
    rig = Rig()
    rig.metrics.fail = True
    result = await rig.verifier.verify(incident(), "orders", None)
    assert result.recovered is False
    assert "could not check" in result.detail
