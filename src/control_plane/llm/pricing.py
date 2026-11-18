"""Cost of a model call from the price table in `plane.yaml` (docs/SPEC.md section 8.2).

Rates are USD per million tokens, one per token class the API reports. A model that is not
in the table is an error: a call must never be priced at zero because nobody wrote its rate.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from control_plane.llm.types import Usage

TOKENS_PER_UNIT = 1_000_000


class PriceTableError(Exception):
    """The price table could not be read: bad YAML, no `prices` key, or a bad entry."""


class UnknownModelError(Exception):
    """A model was priced that is not in the table."""


class ModelPrice(BaseModel):
    """USD per million tokens for one model."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_write: float = Field(ge=0)
    cache_read: float = Field(ge=0)

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, **kwargs)
        except ValidationError as error:
            raise PriceTableError(_describe(error)) from error


class PriceTable:
    def __init__(self, prices: Mapping[str, ModelPrice]) -> None:
        self._prices = dict(prices)

    @classmethod
    def from_plane_yaml(cls, path: Path) -> Self:
        """Read the `prices` key of a `plane.yaml`."""
        try:
            loaded = yaml.safe_load(path.read_text())
        except yaml.YAMLError as error:
            raise PriceTableError(f"{path}: invalid YAML: {error}") from error
        if not isinstance(loaded, Mapping) or "prices" not in loaded:
            raise PriceTableError(f"{path}: no `prices` key")
        return cls.from_mapping(loaded["prices"], source=str(path))

    @classmethod
    def from_mapping(cls, prices: Any, *, source: str = "price table") -> Self:
        """Build a table from the parsed `prices` mapping: model id to its four rates."""
        if not isinstance(prices, Mapping):
            raise PriceTableError(f"{source}: `prices` must be a mapping of model id to rates")
        table: dict[str, ModelPrice] = {}
        for model, rates in prices.items():
            try:
                table[str(model)] = ModelPrice.model_validate(rates)
            except PriceTableError as error:
                raise PriceTableError(f"{source}: prices.{model}: {error}") from error
        return cls(table)

    def __contains__(self, model: object) -> bool:
        return model in self._prices

    def price_for(self, model: str) -> ModelPrice:
        try:
            return self._prices[model]
        except KeyError:
            raise UnknownModelError(self._unknown(model)) from None

    def cost_usd(self, model: str, usage: Usage) -> float:
        """The cost of one call. Raises `UnknownModelError` before looking at the usage."""
        price = self.price_for(model)
        return (
            usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + usage.cache_creation_input_tokens * price.cache_write
            + usage.cache_read_input_tokens * price.cache_read
        ) / TOKENS_PER_UNIT

    def _unknown(self, model: str) -> str:
        if not self._prices:
            return f"no price for model {model!r}: the price table is empty"
        known = ", ".join(sorted(self._prices))
        return f"no price for model {model!r}: the price table has {known}"


def _describe(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        field = ".".join(str(piece) for piece in item["loc"]) or "value"
        parts.append(f"{field}: {item['msg']}")
    return "; ".join(parts)
