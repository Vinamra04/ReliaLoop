"""Loader for the evidence catalog."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

CATALOG_FILE = Path(__file__).resolve().parent / "catalog.yaml"
SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SERVICE_PLACEHOLDER = "$service"


class CatalogError(ValueError):
    """The catalog is invalid, or a query was requested that it does not contain."""


class CatalogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expr: str
    unit: str = ""
    window: str = ""
    core: bool = False
    services: tuple[str, ...] = ()
    expand_by: tuple[str, ...] = ()
    value_label: str | None = None
    description: str

    def applies_to(self, service: str) -> bool:
        return not self.services or service in self.services

    def render(self, service: str) -> str:
        """Substitute the service name, which must be a plain identifier."""
        if not SERVICE_NAME.match(service):
            raise CatalogError(f"invalid service name: {service!r}")
        return self.expr.replace(SERVICE_PLACEHOLDER, service)


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queries: dict[str, CatalogQuery]

    def core(self) -> dict[str, CatalogQuery]:
        return {name: query for name, query in self.queries.items() if query.core}

    def optional_names(self) -> list[str]:
        return sorted(name for name, query in self.queries.items() if not query.core)

    def get(self, name: str) -> CatalogQuery:
        if name not in self.queries:
            raise CatalogError(f"unknown catalog query: {name!r}")
        return self.queries[name]


def load_catalog(path: Path = CATALOG_FILE) -> Catalog:
    """Load and validate the catalog; every expression must be parameterised."""
    try:
        catalog = Catalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise CatalogError(f"invalid evidence catalog {path}: {exc}") from exc
    for name, query in catalog.queries.items():
        if SERVICE_PLACEHOLDER not in query.expr:
            raise CatalogError(f"catalog query {name!r} must reference {SERVICE_PLACEHOLDER}")
    return catalog
