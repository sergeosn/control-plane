"""Input models of the seven tools of docs/SPEC.md section 10.4.

Each model is the contract the model's `tool_use` input is validated against before the
tool runs. Unknown keys are rejected, so a drifted or invented argument is an error the
model sees, not something silently ignored.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileInput(ToolInput):
    path: str = Field(min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _range_is_ordered(self) -> Self:
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must not be smaller than start_line")
        return self


class ListDirInput(ToolInput):
    path: str = Field(min_length=1)


class SearchInput(ToolInput):
    pattern: str = Field(min_length=1)
    glob: str | None = None


class WriteFileInput(ToolInput):
    path: str = Field(min_length=1)
    content: str


class EditFileInput(ToolInput):
    path: str = Field(min_length=1)
    old: str = Field(min_length=1)
    new: str


class DeleteFileInput(ToolInput):
    path: str = Field(min_length=1)


class RunCommandInput(ToolInput):
    argv: list[str] = Field(min_length=1)
    timeout: float | None = Field(default=None, gt=0)
