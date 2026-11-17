"""The base class every domain model shares.

Every model forbids unknown keys, so a file with a misspelt or foreign key is rejected
instead of silently losing data, and so the exported JSON Schema carries
`additionalProperties: false` on every object (SPEC.md section 8.3).

File formats: JSON files are written indented with a trailing newline. JSONL files hold one
compact JSON object per line (`events.jsonl`, `approvals.jsonl`).
"""

from typing import Self

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def to_json(self) -> str:
        """The model as the content of a `.json` file."""
        return self.model_dump_json(indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Parse the content of a `.json` file. Raises `pydantic.ValidationError`."""
        return cls.model_validate_json(text)

    def to_jsonl_line(self) -> str:
        """The model as one line of a `.jsonl` file, newline included."""
        return self.model_dump_json() + "\n"

    @classmethod
    def from_jsonl_line(cls, line: str) -> Self:
        """Parse one line of a `.jsonl` file. Raises `pydantic.ValidationError`."""
        return cls.model_validate_json(line)
