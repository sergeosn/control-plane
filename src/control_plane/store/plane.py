"""Reading `plane.yaml` of the home repository (docs/SPEC.md sections 5 and 14)."""

import yaml
from pydantic import ValidationError

from control_plane.domain.plane import ModelProfile, PlaneConfig
from control_plane.llm.pricing import PriceTable, PriceTableError
from control_plane.store.home import Home

__all__ = ["ModelProfile", "PlaneConfig", "PlaneConfigError", "load_plane", "price_table"]


class PlaneConfigError(Exception):
    """`plane.yaml` is missing or malformed. The message names the file and the field."""


def load_plane(home: Home) -> PlaneConfig:
    path = home.plane_yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlaneConfigError(f"{path}: cannot be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PlaneConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PlaneConfigError(f"{path}: must be a mapping of settings")
    try:
        plane = PlaneConfig.model_validate(data)
        price_table(plane)
    except ValidationError as exc:
        raise PlaneConfigError(f"{path}: {_describe(exc)}") from exc
    except PriceTableError as exc:
        raise PlaneConfigError(f"{path}: {exc}") from exc
    return plane


def price_table(plane: PlaneConfig) -> PriceTable:
    return PriceTable.from_mapping(plane.prices, source="prices")


def _describe(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "(file)"
        message = error["msg"].removeprefix("Value error, ")
        problems.append(message if field == "(file)" else f"{field}: {message}")
    return "; ".join(problems)
