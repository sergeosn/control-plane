"""JSON Schema export for strict tool definitions (SPEC.md section 8.3).

A strict schema needs `additionalProperties: false` on every object and every property listed
in `required`. Optional fields therefore become required-but-nullable, and `default` keywords
are dropped. Constraints such as `minLength` may be ignored by a strict endpoint, which is why
the engine still validates with Pydantic.
"""

from typing import Any

from control_plane.domain.approval import Approval
from control_plane.domain.base import DomainModel
from control_plane.domain.event import Event
from control_plane.domain.manifest import Manifest
from control_plane.domain.plan import Plan
from control_plane.domain.review import Review
from control_plane.domain.task import Task
from control_plane.domain.work_report import WorkReport

MODELS: dict[str, type[DomainModel]] = {
    "task": Task,
    "plan": Plan,
    "work_report": WorkReport,
    "review": Review,
    "approval": Approval,
    "event": Event,
    "manifest": Manifest,
}


def strict_schema(model: type[DomainModel]) -> dict[str, Any]:
    """The model's JSON Schema, tightened for a strict tool definition."""
    return _strictify(model.model_json_schema())


def export_schemas() -> dict[str, dict[str, Any]]:
    """One strict schema per domain model, keyed by artifact name."""
    return {name: strict_schema(model) for name, model in MODELS.items()}


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "default":
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            out[key] = {name: _strictify(child) for name, child in value.items()}
        else:
            out[key] = _strictify(value)
    if out.get("type") == "object" or "properties" in out:
        properties = out.setdefault("properties", {})
        out["required"] = list(properties)
        out["additionalProperties"] = False
    return out
