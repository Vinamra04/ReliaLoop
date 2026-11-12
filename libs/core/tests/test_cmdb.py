from pathlib import Path

import pytest
import yaml

from libs.core.cmdb import Cmdb, CmdbError, load_cmdb
from tools import paths
from tools.slo.cmdb_rules import render_cmdb_rules


def cmdb(**services) -> Cmdb:
    return Cmdb.model_validate({"services": services})


LEAF = {"tier": 0, "owner": "data-team", "managed": False}
APP = {"tier": 1, "owner": "platform-team", "managed": True, "compose_service": "app"}


def test_repository_cmdb_is_valid_and_matches_the_plan():
    loaded = load_cmdb(paths.CMDB_FILE)
    assert set(loaded.services) == {"gateway", "orders", "postgres"}
    assert loaded.services["gateway"].allowed_actions == ("restart_service", "rollback_service")
    assert loaded.services["orders"].limits.max_replicas == 3
    assert loaded.services["postgres"].managed is False
    assert loaded.services["postgres"].allowed_actions == ()


def test_dependency_chain_and_depth():
    loaded = load_cmdb(paths.CMDB_FILE)
    assert loaded.dependency_chain("gateway") == ["gateway", "orders", "postgres"]
    assert loaded.dependency_chain("orders") == ["orders", "postgres"]
    assert [loaded.depth(name) for name in ("gateway", "orders", "postgres")] == [2, 1, 0]


def test_unknown_dependency_is_rejected():
    with pytest.raises(ValueError, match="unknown services: ghost"):
        cmdb(app={**APP, "depends_on": ["ghost"]})


def test_dependency_cycle_is_rejected():
    with pytest.raises(ValueError, match="dependency cycle"):
        cmdb(a={**APP, "depends_on": ["b"]}, b={**APP, "depends_on": ["a"]})


def test_unmanaged_service_cannot_allow_actions():
    with pytest.raises(ValueError, match="unmanaged service"):
        cmdb(db={**LEAF, "allowed_actions": ["restart_service"]})


def test_managed_service_needs_a_compose_service():
    with pytest.raises(ValueError, match="compose_service"):
        cmdb(app={"tier": 1, "owner": "x", "managed": True})


def test_replica_limits_must_be_ordered_and_actions_known():
    with pytest.raises(ValueError, match="max_replicas"):
        cmdb(app={**APP, "limits": {"min_replicas": 3, "max_replicas": 1}})
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic's ValidationError subclass
        cmdb(app={**APP, "allowed_actions": ["delete_everything"]})


def test_load_cmdb_wraps_errors(tmp_path: Path):
    with pytest.raises(CmdbError, match="invalid CMDB"):
        load_cmdb(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("services: {app: {tier: 9}}", encoding="utf-8")
    with pytest.raises(CmdbError):
        load_cmdb(bad)


def test_cmdb_rules_expose_managed_services_and_memory_limits():
    rules = yaml.safe_load(render_cmdb_rules(load_cmdb(paths.CMDB_FILE)))["groups"][0]["rules"]
    managed = {r["labels"]["service"] for r in rules if r["record"] == "service:managed:info"}
    limits = {
        r["labels"]["service"]: r["expr"]
        for r in rules
        if r["record"] == "service:memory_limit:bytes"
    }
    assert managed == {"gateway", "orders"}
    assert limits == {"gateway": "vector(201326592)", "orders": "vector(201326592)"}
