"""`plane.yaml`: model profiles, the price table and defaults (docs/SPEC.md sections 5, 8.5, 14)."""

from typing import Any, Literal, Self

from pydantic import Field, model_validator

from control_plane.domain.base import DomainModel
from control_plane.domain.task import Roles

WireFormat = Literal["anthropic", "openai-compatible"]


class ModelProfile(DomainModel):
    """One model profile: what a role's client is built from."""

    wire_format: WireFormat
    model: str = Field(min_length=1)
    max_tokens: int = Field(default=8192, ge=1)
    temperature: float | None = None
    strict: bool = True


class RunLimits(DomainModel):
    max_iterations: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0)
    max_wall_minutes: int = Field(ge=1)


class StepLimits(DomainModel):
    """The per-step budget of section 8.5, as `agent.StepBudget` takes it."""

    max_turns: int = Field(ge=1)
    max_total_tokens: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0)
    max_wall_seconds: float = Field(gt=0)


class Defaults(DomainModel):
    lane: str = Field(min_length=1)
    roles: Roles
    """The model profile per role a new task gets unless told otherwise."""
    budget: RunLimits
    step_budget: StepLimits


class PlaneConfig(DomainModel):
    version: Literal[1] = 1
    profiles: dict[str, ModelProfile]
    prices: dict[str, dict[str, Any]]
    """Model id to its four rates; `llm.PriceTable` validates the rates."""
    defaults: Defaults

    @model_validator(mode="after")
    def _every_profile_is_priced(self) -> Self:
        for name, profile in self.profiles.items():
            if profile.model not in self.prices:
                raise ValueError(
                    f"profiles.{name}: model {profile.model!r} has no entry under prices"
                )
        for role, profile in self.defaults.roles.model_dump().items():
            if profile not in self.profiles:
                raise ValueError(f"defaults.roles.{role}: no profile {profile!r}")
        return self
