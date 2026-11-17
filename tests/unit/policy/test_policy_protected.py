"""Task 7: protected-path detection (SPEC.md section 10.2)."""

import pytest

from control_plane.policy import ProtectedPaths

PATTERNS = [
    "src/control_plane/policy/",
    "tests/guardrails",
    "SPEC.md",
    "*.lock",
    ".github/workflows/*.yml",
]


@pytest.fixture
def protected() -> ProtectedPaths:
    return ProtectedPaths(PATTERNS)


@pytest.mark.parametrize(
    "path",
    [
        "src/control_plane/policy/classify.py",
        "src/control_plane/policy/deep/er.py",
        "tests/guardrails/test_guardrail_policy.py",
        "tests/guardrails",
        "SPEC.md",
        "uv.lock",
        ".github/workflows/ci.yml",
        "./SPEC.md",
    ],
)
def test_a_protected_path_matches(protected: ProtectedPaths, path: str) -> None:
    assert protected.matches(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/control_plane/policy.py",
        "src/control_plane/policyx/a.py",
        "tests/guardrails_extra/t.py",
        "docs/SPEC.md",
        "SPEC.md.bak",
        "uv.lock.old",
        ".github/workflows/ci.yaml",
        "",
    ],
)
def test_an_ordinary_path_does_not_match(protected: ProtectedPaths, path: str) -> None:
    assert not protected.matches(path)


def test_touched_lists_the_protected_files_sorted_and_once(protected: ProtectedPaths) -> None:
    touched = protected.touched(
        ["src/a.py", "uv.lock", "SPEC.md", "uv.lock", "src/control_plane/policy/rules.py"]
    )

    assert touched == ["SPEC.md", "src/control_plane/policy/rules.py", "uv.lock"]


def test_no_patterns_protects_nothing() -> None:
    empty = ProtectedPaths([])

    assert not empty
    assert not empty.matches("SPEC.md")
    assert empty.touched(["SPEC.md"]) == []


def test_from_project_reads_the_protected_paths_key() -> None:
    assert ProtectedPaths.from_project({"protected_paths": ["SPEC.md"]}).patterns == ("SPEC.md",)
    assert ProtectedPaths.from_project({}).patterns == ()


def test_from_project_rejects_a_non_list() -> None:
    with pytest.raises(ValueError, match="protected_paths"):
        ProtectedPaths.from_project({"protected_paths": "SPEC.md"})
