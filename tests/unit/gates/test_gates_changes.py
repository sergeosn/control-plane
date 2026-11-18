"""Task 13: the gates that read the diff, `diff_size` and `secrets` (docs/SPEC.md section 11).

Both look at every change in the worktree against the base commit: committed, uncommitted
and untracked alike, as `diff.patch` does.
"""

from pathlib import Path

import pytest

from control_plane.domain.project import SecretsGate
from control_plane.gates import GateContext, run_gate
from control_plane.gates.changes import DEFAULT_SECRET_PATTERNS, secret_matches
from control_plane.store.git import Git
from control_plane.workspace import Worktree, commit, create_worktree
from fixtures.gate_repo import CALC, gate_project, make_gate_project
from fixtures.project_repo import write

RUN_ID = "R-20261117-150000-ab78"

# Fake credentials, assembled at run time so that this file holds no literal secret.
AWS_KEY = "AKIA" + "Q" * 16
GITHUB_TOKEN = "ghp_" + "a1B2" * 9
PRIVATE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----"
ANTHROPIC_KEY = "sk-ant-" + "api03-" + "x" * 30


@pytest.fixture
def project(tmp_path: Path) -> Git:
    return make_gate_project(tmp_path / "project")


@pytest.fixture
def tree(project: Git, tmp_path: Path) -> Worktree:
    return create_worktree(project.root, tmp_path / "home" / ".worktrees", RUN_ID, project.head())


def ctx(tree: Worktree, **settings: object) -> GateContext:
    return GateContext(tree=tree, project=gate_project(tree.repo, **settings))


# --- diff_size ----------------------------------------------------------------------------


async def test_diff_size_passes_within_the_limit(tree: Worktree) -> None:
    write(tree.path, {"notes.txt": "first line\nsecond line\n"})

    result = await run_gate("diff_size", ctx(tree, max_lines=5))

    assert result.status == "pass", result.output
    assert "changed lines: 1" in result.summary


async def test_diff_size_fails_over_the_limit(tree: Worktree) -> None:
    write(tree.path, {"big.txt": "".join(f"line {n}\n" for n in range(10))})

    result = await run_gate("diff_size", ctx(tree, max_lines=5))

    assert result.status == "fail"
    assert "changed lines: 10" in result.summary
    assert "limit 5" in result.summary
    assert "big.txt" in result.output


async def test_diff_size_counts_committed_and_uncommitted_changes_together(
    tree: Worktree,
) -> None:
    write(tree.path, {"a.txt": "1\n2\n3\n"})
    commit(tree, "First half")
    write(tree.path, {"notes.txt": "changed\n"})

    result = await run_gate("diff_size", ctx(tree, max_lines=4))

    assert result.status == "fail"
    assert "changed lines: 5" in result.summary


# --- secrets ------------------------------------------------------------------------------


async def test_secrets_passes_when_no_added_line_matches(tree: Worktree) -> None:
    write(tree.path, {"calc.py": CALC + "\n\nTOKEN_HEADER = 'Authorization'\n"})

    result = await run_gate("secrets", ctx(tree))

    assert result.status == "pass", result.output


async def test_secrets_fails_when_an_added_line_matches_and_never_repeats_the_secret(
    tree: Worktree,
) -> None:
    write(tree.path, {"config.py": f"ID = 1\nAWS_KEY = '{AWS_KEY}'\n"})

    result = await run_gate("secrets", ctx(tree))

    assert result.status == "fail"
    assert "config.py:2: aws_access_key_id" in result.output
    assert AWS_KEY not in result.output
    assert AWS_KEY not in result.summary


async def test_secrets_reports_the_line_number_in_a_changed_file(tree: Worktree) -> None:
    write(tree.path, {"notes.txt": f"first line\nsecond\ntoken {GITHUB_TOKEN}\n"})

    result = await run_gate("secrets", ctx(tree))

    assert result.status == "fail"
    assert "notes.txt:3: github_token" in result.output


async def test_secrets_ignores_a_secret_that_was_already_in_the_base(
    project: Git, tmp_path: Path
) -> None:
    write(project.root, {"legacy.txt": f"{AWS_KEY}\n"})
    project.add(".")
    project.commit("Legacy secret")
    tree = create_worktree(project.root, tmp_path / "wt", "R-later", project.head())
    write(tree.path, {"legacy.txt": f"{AWS_KEY}\nmore\n"})

    result = await run_gate("secrets", ctx(tree))

    assert result.status == "pass", result.output


async def test_secrets_ignores_a_removed_line(project: Git, tmp_path: Path) -> None:
    write(project.root, {"legacy.txt": f"{AWS_KEY}\n"})
    project.add(".")
    project.commit("Legacy secret")
    tree = create_worktree(project.root, tmp_path / "wt", "R-later", project.head())
    (tree.path / "legacy.txt").unlink()

    result = await run_gate("secrets", ctx(tree))

    assert result.status == "pass", result.output


async def test_secrets_uses_the_extra_patterns_of_project_yaml(tree: Worktree) -> None:
    project = gate_project(tree.repo)
    project.gates.secrets = SecretsGate(patterns={"internal_token": r"itk_[a-z0-9]{8}"})
    write(tree.path, {"notes.txt": "itk_abcd1234\n"})

    result = await run_gate("secrets", GateContext(tree=tree, project=project))

    assert result.status == "fail"
    assert "notes.txt:1: internal_token" in result.output


async def test_secrets_skips_the_ignored_paths_of_project_yaml(tree: Worktree) -> None:
    project = gate_project(tree.repo)
    project.gates.secrets = SecretsGate(ignore_paths=["tests/fixtures/"])
    write(tree.path, {"tests/fixtures/keys.txt": f"{AWS_KEY}\n"})

    result = await run_gate("secrets", GateContext(tree=tree, project=project))

    assert result.status == "pass", result.output


@pytest.mark.parametrize(
    ("line", "name"),
    [
        (f"key = '{AWS_KEY}'", "aws_access_key_id"),
        (PRIVATE_KEY, "private_key"),
        (f"export GH={GITHUB_TOKEN}", "github_token"),
        (f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}", "anthropic_api_key"),
        ("slack = 'xoxb-" + "1234567890-abcdefghij'", "slack_token"),
        ("AIza" + "B" * 35, "google_api_key"),
        ("password = '" + "hunter2hunter2!'", "credential_assignment"),
    ],
)
def test_each_default_pattern_matches_its_kind_of_secret(line: str, name: str) -> None:
    assert name in secret_matches(line, DEFAULT_SECRET_PATTERNS)


@pytest.mark.parametrize(
    "line",
    [
        "def password_is_valid(password):",
        "token = request.headers['Authorization']",
        "API_KEY_ENV = 'ANTHROPIC_API_KEY'",
        "sk = 'sk-short'",
    ],
)
def test_ordinary_code_matches_no_default_pattern(line: str) -> None:
    assert secret_matches(line, DEFAULT_SECRET_PATTERNS) == []
