"""API payload models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AlertStatus = Literal["firing", "resolved"]

# Alerts carrying this label (value "true") exercise the delivery path only:
# they are counted and acknowledged but never become incidents or tickets.
TEST_ALERT_LABEL = "relialoop_test"


class AmAlert(BaseModel):
    """One alert inside an Alertmanager webhook notification (payload version 4)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: AlertStatus
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    generator_url: str = Field(default="", alias="generatorURL")
    fingerprint: str = Field(min_length=1, max_length=64)

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "")

    @property
    def is_test(self) -> bool:
        return self.labels.get(TEST_ALERT_LABEL) == "true"


class AmWebhook(BaseModel):
    """Alertmanager webhook notification: a group of alerts."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    version: str = "4"
    group_key: str = Field(alias="groupKey", min_length=1, max_length=2048)
    status: AlertStatus
    receiver: str = ""
    group_labels: dict[str, str] = Field(default_factory=dict, alias="groupLabels")
    common_labels: dict[str, str] = Field(default_factory=dict, alias="commonLabels")
    common_annotations: dict[str, str] = Field(default_factory=dict, alias="commonAnnotations")
    external_url: str = Field(default="", alias="externalURL")
    truncated_alerts: int = Field(default=0, alias="truncatedAlerts")
    alerts: list[AmAlert] = Field(min_length=1)


class Accepted(BaseModel):
    """Response to an accepted webhook."""

    accepted: int
    ignored_test_alerts: int = 0
