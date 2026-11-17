"""Task 7: the rules loader (SPEC.md section 10.3).

`rules/tools.yaml` is loaded with validation. A malformed file is rejected at load with the
position of the error, so a human can fix it without guessing.
"""

from pathlib import Path

import pytest

from control_plane.policy import (
    CLASS_DECISIONS,
    Rule,
    Rules,
    RulesError,
    load_rules,
    parse_rules,
)
from control_plane.store.home import TEMPLATE_DIR

DEFAULT_RULES = TEMPLATE_DIR / "rules" / "tools.yaml"

MINIMAL = """\
version: 1
default: ask
rules:
  - id: reads
    tool: [read_file, list_dir]
    class: read
  - id: commands
    tool: run_command
    argv_prefix: [[pytest], [git, status]]
    class: write-local
"""


def test_the_default_rules_file_loads() -> None:
    rules = load_rules(DEFAULT_RULES)

    assert rules.version == 1
    assert rules.default == "ask"
    assert [rule.id for rule in rules.rules] == [
        "read-tools",
        "local-file-writes",
        "local-commands",
        "network-and-deps",
        "history-rewrite",
        "never",
    ]


def test_a_single_tool_name_is_normalised_to_a_list() -> None:
    rules = parse_rules(MINIMAL)

    assert rules.rules[0].tool == ["read_file", "list_dir"]
    assert rules.rules[1].tool == ["run_command"]
    assert rules.rules[1].argv_prefix == [["pytest"], ["git", "status"]]
    assert rules.rules[0].argv_prefix is None


def test_every_class_maps_to_a_decision() -> None:
    assert CLASS_DECISIONS == {
        "read": "allow",
        "write-local": "allow",
        "write-external": "park",
        "destructive": "park",
        "blocked": "deny",
    }


def test_rules_round_trip_through_a_mapping() -> None:
    rules = parse_rules(MINIMAL)

    assert Rules.model_validate(rules.model_dump(by_alias=True)) == rules


def test_load_names_the_file_in_the_error(tmp_path: Path) -> None:
    path = tmp_path / "tools.yaml"
    path.write_text("version: 1\ndefault: ask\nrules: []\n")

    with pytest.raises(RulesError) as info:
        load_rules(path)

    assert str(info.value).startswith(f"{path}:")


def test_a_missing_file_is_a_rules_error(tmp_path: Path) -> None:
    with pytest.raises(RulesError, match="tools.yaml"):
        load_rules(tmp_path / "tools.yaml")


@pytest.mark.parametrize(
    ("text", "line", "column", "fragment"),
    [
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n  - id: x\n    tool: [read_file\n",
            5,
            11,
            "",
            id="invalid-yaml-syntax",
        ),
        pytest.param("- a\n- b\n", 1, 1, "mapping", id="not-a-mapping"),
        pytest.param("", 1, 1, "mapping", id="empty-file"),
        pytest.param(
            "version: 2\ndefault: ask\nrules:\n  - {id: x, tool: read_file, class: read}\n",
            1,
            10,
            "version",
            id="unsupported-version",
        ),
        pytest.param(
            "version: 1\ndefault: allow\nrules:\n  - {id: x, tool: read_file, class: read}\n",
            2,
            10,
            "default",
            id="unknown-default",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules: []\n",
            3,
            8,
            "rules",
            id="no-rules",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules: all\n",
            3,
            8,
            "list",
            id="rules-not-a-list",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n  - id: x\n    tool: read_file\n    class: nuke\n",
            6,
            12,
            "class",
            id="unknown-class",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n  - id: x\n    tool: teleport\n    class: read\n",
            5,
            11,
            "teleport",
            id="unknown-tool",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n  - tool: read_file\n    class: read\n",
            4,
            5,
            "id",
            id="missing-id",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n"
            "  - id: a\n    tool: read_file\n    class: read\n"
            "  - id: a\n    tool: list_dir\n    class: read\n",
            7,
            9,
            "duplicate",
            id="duplicate-id",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n"
            "  - id: x\n    tool: read_file\n    argv_prefix: [[ls]]\n    class: read\n",
            6,
            18,
            "run_command",
            id="argv-prefix-on-a-file-tool",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n"
            "  - id: x\n    tool: run_command\n    argv_prefix: [[]]\n    class: read\n",
            6,
            19,
            "at least 1 item",
            id="empty-argv-prefix",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n"
            "  - id: x\n    tool: run_command\n    argv_prefix: [[git, 1]]\n    class: read\n",
            6,
            25,
            "string",
            id="non-string-in-prefix",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n"
            "  - id: x\n    tool: read_file\n    class: read\n    when: always\n",
            7,
            5,
            "when",
            id="unknown-key",
        ),
        pytest.param(
            "version: 1\ndefault: ask\nrules:\n  - id: x\n    tool: read_file\n",
            4,
            5,
            "class",
            id="missing-class",
        ),
    ],
)
def test_a_malformed_rules_file_is_rejected_with_its_position(
    text: str, line: int, column: int, fragment: str
) -> None:
    with pytest.raises(RulesError) as info:
        parse_rules(text, source="tools.yaml")

    error = info.value
    assert (error.line, error.column) == (line, column), str(error)
    assert str(error).startswith(f"tools.yaml:{line}:{column}: ")
    assert fragment in str(error)


def test_a_rule_id_must_be_a_slug() -> None:
    text = MINIMAL.replace("id: reads", "id: 'bad id'")

    with pytest.raises(RulesError, match=r"tools.yaml:4:9: .*id"):
        parse_rules(text, source="tools.yaml")


def test_duplicate_ids_are_rejected_in_code_too() -> None:
    rules = parse_rules(MINIMAL)
    data = rules.model_dump(by_alias=True)
    data["rules"][1]["id"] = "reads"

    with pytest.raises(ValueError, match="duplicate rule id 'reads'"):
        Rules.model_validate(data)


def test_a_rule_matches_its_tool_and_one_of_its_prefixes() -> None:
    files = Rule(id="files", tool=["write_file"], **{"class": "write-local"})
    commands = Rule(
        id="commands",
        tool="run_command",
        argv_prefix=[["pytest"], ["git", "status"]],
        **{"class": "write-local"},
    )

    assert files.matches("write_file", None)
    assert not files.matches("read_file", None)
    assert commands.matches("run_command", ["git", "status", "-s"])
    assert not commands.matches("run_command", ["git", "push"])
    assert not commands.matches("run_command", None)
