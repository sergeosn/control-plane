"""A lane on disk: procedure, rubric, role prompts and flow (docs/SPEC.md sections 4 and 5).

`load_lane` reads `lanes/<name>/` of the home repository. The roles a lane defines are the
prompts under `roles/`; its flow is validated against them. The rubric is a list of rows,
each `- [ID] text`, and the reviewer grades every row by its id (section 8.4).
"""

import re
from dataclasses import dataclass
from pathlib import Path

from control_plane.flow.schema import Flow, load_flow

PROCEDURE_FILE = "procedure.md"
RUBRIC_FILE = "rubric.md"
FLOW_FILE = "flow.yaml"
ROLES_DIR = "roles"

_ROW = re.compile(r"^- \[([A-Za-z0-9_-]+)\] \S")


class LaneError(ValueError):
    """A lane is missing a file or its rubric is malformed."""


@dataclass(frozen=True)
class Lane:
    name: str
    path: Path
    flow: Flow
    roles: frozenset[str]
    rubric_rows: tuple[str, ...]

    def role_prompt(self, role: str) -> Path:
        return self.path / ROLES_DIR / f"{role}.md"


def rubric_row_ids(text: str) -> tuple[str, ...]:
    """The row ids of a rubric, in order. Raises `LaneError` on none or a duplicate."""
    rows: list[str] = []
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        if match.group(1) in rows:
            raise LaneError(f"{RUBRIC_FILE}: duplicate rubric row id {match.group(1)!r}")
        rows.append(match.group(1))
    if not rows:
        raise LaneError(f"{RUBRIC_FILE} has no rows of the form '- [ID] text'")
    return tuple(rows)


def load_lane(path: Path) -> Lane:
    """Read and validate the lane at `path`. Raises `LaneError` or `FlowValidationError`."""
    for name in (PROCEDURE_FILE, RUBRIC_FILE, FLOW_FILE):
        if not (path / name).is_file():
            raise LaneError(f"lane {path.name} has no {name}: {path / name}")
    roles = frozenset(prompt.stem for prompt in (path / ROLES_DIR).glob("*.md"))
    rows = rubric_row_ids((path / RUBRIC_FILE).read_text(encoding="utf-8"))
    flow = load_flow(path / FLOW_FILE, roles)
    return Lane(name=path.name, path=path, flow=flow, roles=roles, rubric_rows=rows)
