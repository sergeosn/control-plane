"""`manifest.json`: what a run was given (docs/SPEC.md section 9).

Hashes make the run reproducible in the sense that its exact inputs can be reconstructed.
"""

from pydantic import AwareDatetime, Field

from control_plane.domain.base import DomainModel


class FileHash(DomainModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RoleModel(DomainModel):
    """The model id and parameters one role ran with."""

    role: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    model: str = Field(min_length=1)
    max_tokens: int = Field(ge=1)
    temperature: float | None = None


class RunBudget(DomainModel):
    max_iterations: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0)
    max_wall_minutes: int = Field(ge=1)


class Manifest(DomainModel):
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_file: FileHash
    project: str = Field(min_length=1)
    project_base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    engine_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    flow: FileHash
    rubric: FileHash
    role_prompts: list[FileHash]
    rules: list[FileHash]
    models: list[RoleModel]
    budget: RunBudget
    context_files: list[FileHash]
    created_at: AwareDatetime
