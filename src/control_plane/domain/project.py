"""`project.yaml`: a project's repository, protected paths and gate commands (docs/SPEC.md 5, 11).

The file lives in the home repository, never in the project's worktree, so an agent cannot
edit a gate into passing. A command gate that is not configured is skipped and recorded as
such; `test_integrity`, `diff_size` and `secrets` need no command and always run, with the
defaults below unless the file overrides them.
"""

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from control_plane.domain.base import DomainModel

REPORT_PLACEHOLDER = "{report}"
DEFAULT_GATE_TIMEOUT_S = 30 * 60.0
CONTROL_SUFFIX = "-control"


class CommandGate(DomainModel):
    """A gate that runs `argv` in the run worktree and fails on a non-zero exit."""

    argv: list[str] = Field(min_length=1)
    timeout_s: float = Field(default=DEFAULT_GATE_TIMEOUT_S, gt=0)


class CoverageGate(CommandGate):
    """Runs `argv`, which writes a coverage.py JSON report to `{report}`, and reads it.

    `{report}` is replaced by a path outside the worktree. The gate fails when the report's
    total is under `threshold`, which lives here and not in the worktree's own configuration.
    """

    threshold: float = Field(ge=0, le=100)

    @field_validator("argv")
    @classmethod
    def _names_the_report(cls, argv: list[str]) -> list[str]:
        if not any(REPORT_PLACEHOLDER in part for part in argv):
            raise ValueError(f"the coverage command must contain {REPORT_PLACEHOLDER}")
        return argv


class GuardrailGate(CommandGate):
    """Runs `argv` in the control checkout with the worktree's `source` first on the import path.

    `control` defaults to the project repository's path with `-control` appended.
    """

    control: Path | None = None
    source: str = "src"


class IntegrityGate(DomainModel):
    """Which files hold tests and markers: a file name glob, or a path glob with a slash."""

    patterns: list[str] = Field(default_factory=lambda: ["test_*.py", "*_test.py", "conftest.py"])


class DiffSizeGate(DomainModel):
    max_changed_lines: int = Field(default=1000, ge=1)


class SecretsGate(DomainModel):
    """Extra secret patterns by name, and paths (file, directory or glob) the scan skips."""

    patterns: dict[str, str] = Field(default_factory=dict)
    ignore_paths: list[str] = Field(default_factory=list)

    @field_validator("patterns")
    @classmethod
    def _compiles(cls, patterns: dict[str, str]) -> dict[str, str]:
        for name, pattern in patterns.items():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"pattern {name!r} is not a regular expression: {exc}") from exc
        return patterns


class GateSettings(DomainModel):
    format: CommandGate | None = None
    lint: CommandGate | None = None
    tests: CommandGate | None = None
    coverage: CoverageGate | None = None
    test_integrity: IntegrityGate = Field(default_factory=IntegrityGate)
    diff_size: DiffSizeGate = Field(default_factory=DiffSizeGate)
    secrets: SecretsGate = Field(default_factory=SecretsGate)
    guardrail_regression: GuardrailGate | None = None


class ProjectConfig(DomainModel):
    version: Literal[1] = 1
    repo: Path
    """The primary checkout of the project, on `main`."""
    protected_paths: list[str] = Field(default_factory=list)
    gates: GateSettings = Field(default_factory=GateSettings)

    def control_checkout(self) -> Path:
        """The control checkout (section 13): the guardrail gate's `control` when it names
        one, else the repository's path with `-control` appended. `cplane self-update` moves
        it; the guardrail gate takes its suite from it."""
        gate = self.gates.guardrail_regression
        if gate is not None and gate.control is not None:
            return gate.control
        return self.repo.with_name(self.repo.name + CONTROL_SUFFIX)

    def guardrail_control(self) -> Path | None:
        """The control checkout the guardrail suite is taken from; None without that gate."""
        if self.gates.guardrail_regression is None:
            return None
        return self.control_checkout()
