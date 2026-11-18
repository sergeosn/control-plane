"""Task 2: every exported JSON Schema is valid for a strict tool definition (docs/SPEC.md 8.3)."""

from collections.abc import Iterator
from typing import Any

import pytest

from control_plane.domain import (
    Approval,
    Event,
    Manifest,
    Plan,
    Review,
    Task,
    WorkReport,
    export_schemas,
    strict_schema,
)

ARTIFACTS = {"plan": Plan, "work_report": WorkReport, "review": Review}
ALL_MODELS = {
    **ARTIFACTS,
    "task": Task,
    "approval": Approval,
    "event": Event,
    "manifest": Manifest,
}


def objects_in(schema: Any) -> Iterator[dict[str, Any]]:
    """Yield every schema node that describes an object, including those under `$defs`."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" or "properties" in schema:
            yield schema
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                for prop in value.values():
                    yield from objects_in(prop)
            else:
                yield from objects_in(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from objects_in(item)


def test_export_covers_every_model() -> None:
    assert set(export_schemas()) == set(ALL_MODELS)


@pytest.mark.parametrize("name", sorted(ALL_MODELS))
def test_every_object_in_every_exported_schema_forbids_additional_properties(name: str) -> None:
    schema = export_schemas()[name]

    found = list(objects_in(schema))
    assert found, "schema has no object nodes"
    for node in found:
        assert node.get("additionalProperties") is False, node


@pytest.mark.parametrize("name", sorted(ALL_MODELS))
def test_every_object_lists_its_properties_as_required(name: str) -> None:
    """Strict mode treats absent keys as errors, so required must name every property."""
    for node in objects_in(export_schemas()[name]):
        assert set(node.get("required", [])) == set(node["properties"]), node


@pytest.mark.parametrize("name", sorted(ALL_MODELS))
def test_exported_schema_has_no_default_keyword(name: str) -> None:
    def defaults_in(schema: Any) -> Iterator[Any]:
        if isinstance(schema, dict):
            for key, value in schema.items():
                if key == "default":
                    yield value
                elif key == "properties":
                    for prop in value.values():
                        yield from defaults_in(prop)
                else:
                    yield from defaults_in(value)
        elif isinstance(schema, list):
            for item in schema:
                yield from defaults_in(item)

    assert list(defaults_in(export_schemas()[name])) == []


@pytest.mark.parametrize(("name", "model"), sorted(ARTIFACTS.items()))
def test_artifact_schema_is_a_strict_tool_input_schema(name: str, model: type) -> None:
    schema = strict_schema(model)

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema == export_schemas()[name]


def test_review_schema_has_no_verdict() -> None:
    assert "verdict" not in strict_schema(Review)["properties"]
