"""Task 7: `classify(role, call)` (docs/SPEC.md section 10.3).

Evaluation order: hard checks (role exposure, well-formed argv) first, then the first
matching rule, then the default. Every decision carries the rule id that produced it and is
logged as an event.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from control_plane.domain import Event, ToolCall
from control_plane.llm.types import ToolUseBlock
from control_plane.policy import Classification, Policy, load_rules, parse_rules
from control_plane.store.home import TEMPLATE_DIR, create_home
from control_plane.store.runs import RunStore

DEFAULT_RULES = TEMPLATE_DIR / "rules" / "tools.yaml"
TS = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def call(tool: str, **arguments: Any) -> ToolUseBlock:
    return ToolUseBlock(id="toolu_01", name=tool, input=arguments)


@pytest.fixture
def policy() -> Policy:
    return Policy(load_rules(DEFAULT_RULES))


# Every rule of the default tools.yaml, every argv prefix included: (rule id, tool, argv).
DEFAULT_TABLE: list[tuple[str, str, list[str] | None, str, str]] = [
    ("read-tools", "read_file", None, "read", "allow"),
    ("read-tools", "list_dir", None, "read", "allow"),
    ("read-tools", "search", None, "read", "allow"),
    ("local-file-writes", "write_file", None, "write-local", "allow"),
    ("local-file-writes", "edit_file", None, "write-local", "allow"),
    ("local-file-writes", "delete_file", None, "write-local", "allow"),
    ("local-commands", "run_command", ["pytest", "-q"], "write-local", "allow"),
    ("local-commands", "run_command", ["ruff", "check", "."], "write-local", "allow"),
    ("local-commands", "run_command", ["uv", "run", "pytest"], "write-local", "allow"),
    ("local-commands", "run_command", ["uv", "run", "ruff", "format"], "write-local", "allow"),
    ("local-commands", "run_command", ["git", "status"], "write-local", "allow"),
    ("local-commands", "run_command", ["git", "diff", "--stat"], "write-local", "allow"),
    ("local-commands", "run_command", ["git", "add", "-A"], "write-local", "allow"),
    ("local-commands", "run_command", ["git", "commit", "-m", "x"], "write-local", "allow"),
    ("network-and-deps", "run_command", ["git", "push", "origin"], "write-external", "park"),
    ("network-and-deps", "run_command", ["gh", "pr", "create"], "write-external", "park"),
    ("network-and-deps", "run_command", ["curl", "https://x"], "write-external", "park"),
    ("network-and-deps", "run_command", ["uv", "add", "httpx"], "write-external", "park"),
    ("network-and-deps", "run_command", ["pip", "install", "x"], "write-external", "park"),
    ("history-rewrite", "run_command", ["git", "reset", "--hard"], "destructive", "park"),
    ("history-rewrite", "run_command", ["git", "clean", "-fd"], "destructive", "park"),
    ("history-rewrite", "run_command", ["git", "checkout", "--", "a.py"], "destructive", "park"),
    ("history-rewrite", "run_command", ["rm", "-rf", "build"], "destructive", "park"),
    ("never", "run_command", ["sudo", "ls"], "blocked", "deny"),
    ("never", "run_command", ["ssh", "host"], "blocked", "deny"),
    ("never", "run_command", ["env"], "blocked", "deny"),
    ("never", "run_command", ["printenv", "PATH"], "blocked", "deny"),
]


def _arguments(tool: str, argv: list[str] | None) -> dict[str, Any]:
    if tool == "run_command":
        return {"argv": argv}
    return {"path": "src/a.py", "content": "x", "old": "a", "new": "b", "pattern": "a"}


@pytest.mark.parametrize(
    ("rule_id", "tool", "argv", "tool_class", "decision"),
    DEFAULT_TABLE,
    ids=[f"{row[0]}:{row[1]}:{' '.join(row[2] or [])}" for row in DEFAULT_TABLE],
)
def test_every_rule_of_the_default_rules_file(
    policy: Policy,
    rule_id: str,
    tool: str,
    argv: list[str] | None,
    tool_class: str,
    decision: str,
) -> None:
    result = policy.classify("worker", call(tool, **_arguments(tool, argv)))

    assert (result.rule_id, result.tool_class, result.decision) == (rule_id, tool_class, decision)


def test_the_table_covers_every_rule_and_every_prefix() -> None:
    rules = load_rules(DEFAULT_RULES)
    covered = {(row[0], row[1], tuple(row[2] or ())) for row in DEFAULT_TABLE}

    for rule in rules.rules:
        for tool in rule.tool:
            if rule.argv_prefix is None:
                assert (rule.id, tool, ()) in covered, (rule.id, tool)
                continue
            for prefix in rule.argv_prefix:
                assert any(
                    rid == rule.id and t == tool and argv[: len(prefix)] == tuple(prefix)
                    for rid, t, argv in covered
                ), (rule.id, prefix)


def test_a_command_no_rule_matches_is_parked_by_the_default(policy: Policy) -> None:
    result = policy.classify("worker", call("run_command", argv=["make", "test"]))

    assert result.decision == "park"
    assert result.tool_class is None
    assert result.rule_id is None
    assert "no rule matches" in result.reason
    assert "make test" in result.reason


def test_a_blocked_command_is_denied(policy: Policy) -> None:
    result = policy.classify("worker", call("run_command", argv=["sudo", "rm", "-rf", "/"]))

    assert result.decision == "deny"
    assert result.rule_id == "never"
    assert "blocked" in result.reason and "never" in result.reason


def test_a_prefix_matches_only_whole_leading_arguments(policy: Policy) -> None:
    assert policy.classify("worker", call("run_command", argv=["git"])).rule_id is None
    assert policy.classify("worker", call("run_command", argv=["gitk"])).rule_id is None
    assert policy.classify("worker", call("run_command", argv=["pytest"])).rule_id == (
        "local-commands"
    )
    checkout = policy.classify("worker", call("run_command", argv=["git", "checkout", "-b", "x"]))
    assert checkout.rule_id is None


def test_the_first_matching_rule_wins() -> None:
    policy = Policy(
        parse_rules(
            "version: 1\ndefault: ask\nrules:\n"
            "  - {id: first, tool: run_command, argv_prefix: [[git]], class: blocked}\n"
            "  - {id: second, tool: run_command, argv_prefix: [[git, status]], class: read}\n"
        )
    )

    result = policy.classify("worker", call("run_command", argv=["git", "status"]))

    assert (result.rule_id, result.decision) == ("first", "deny")


def test_a_default_of_deny_denies_an_unmatched_call() -> None:
    policy = Policy(
        parse_rules(
            "version: 1\ndefault: deny\nrules:\n"
            "  - {id: reads, tool: [read_file, list_dir, search], class: read}\n"
        )
    )

    result = policy.classify("worker", call("run_command", argv=["make"]))

    assert (result.decision, result.rule_id, result.tool_class) == ("deny", None, None)


@pytest.mark.parametrize("role", ["planner", "reviewer"])
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_file", {"path": "a.py", "content": "x"}),
        ("edit_file", {"path": "a.py", "old": "a", "new": "b"}),
        ("delete_file", {"path": "a.py"}),
        ("run_command", {"argv": ["pytest"]}),
    ],
)
def test_a_role_calling_a_tool_it_does_not_have_is_denied_before_any_rule(
    policy: Policy, role: str, tool: str, arguments: dict[str, Any]
) -> None:
    result = policy.classify(role, call(tool, **arguments))

    assert result.decision == "deny"
    assert result.rule_id is None
    assert result.tool_class is None
    assert tool in result.reason and role in result.reason


def test_an_unknown_tool_is_denied(policy: Policy) -> None:
    result = policy.classify("worker", call("teleport", to="prod"))

    assert result.decision == "deny"
    assert "teleport" in result.reason


def test_an_own_submit_tool_is_allowed_without_a_rule(policy: Policy) -> None:
    result = policy.classify("planner", call("submit_plan", summary="x"))

    assert (result.decision, result.rule_id, result.tool_class) == ("allow", None, None)
    assert policy.classify("planner", call("submit_review")).decision == "deny"


@pytest.mark.parametrize("argv", [[], "pytest", ["pytest", 1], None])
def test_a_malformed_argv_is_denied_not_parked(policy: Policy, argv: Any) -> None:
    arguments = {} if argv is None else {"argv": argv}

    result = policy.classify("worker", call("run_command", **arguments))

    assert result.decision == "deny"
    assert result.rule_id is None
    assert "argv" in result.reason


def test_an_unknown_role_is_an_error(policy: Policy) -> None:
    with pytest.raises(ValueError, match="unknown role"):
        policy.classify("admin", call("read_file", path="a"))


def test_the_decision_uses_only_role_name_and_arguments(policy: Policy) -> None:
    a = policy.classify("worker", call("run_command", argv=["git", "push"]))
    b = policy.classify(
        "worker",
        ToolUseBlock(
            id="other",
            name="run_command",
            input={
                "argv": ["git", "push"],
                "timeout": 5,
            },
        ),
    )

    assert (a.decision, a.rule_id) == (b.decision, b.rule_id) == ("park", "network-and-deps")


@pytest.fixture
def policy_log() -> list[Classification]:
    return []


def test_every_decision_is_handed_to_the_log(policy_log: list[Classification]) -> None:
    policy = Policy(load_rules(DEFAULT_RULES), log=policy_log.append)

    policy.classify("worker", call("read_file", path="a"))
    policy.classify("planner", call("write_file", path="a", content="x"))
    policy.classify("worker", call("run_command", argv=["make"]))

    assert [d.decision for d in policy_log] == ["allow", "deny", "park"]
    assert [d.rule_id for d in policy_log] == ["read-tools", None, None]


def test_a_decision_becomes_an_event_and_a_tool_call_record(policy: Policy) -> None:
    decision = policy.classify("worker", call("run_command", argv=["git", "push"]))

    fields = decision.event_fields()
    record = decision.to_tool_call(ts=TS, tool_use_id="toolu_01")

    assert fields == {"kind": "tool_decision", "outcome": "park", "message": decision.reason}
    assert "network-and-deps" in decision.reason
    assert record == ToolCall(
        ts=TS,
        tool_use_id="toolu_01",
        tool="run_command",
        arguments={"argv": ["git", "push"]},
        tool_class="write-external",
        decision="park",
        rule_id="network-and-deps",
        error=decision.reason,
    )


def test_an_allowed_call_record_has_no_error(policy: Policy) -> None:
    decision = policy.classify("worker", call("read_file", path="a"))

    assert decision.to_tool_call(ts=TS, tool_use_id="t").error is None


def test_a_decision_is_logged_into_the_run_folder(tmp_path: Path) -> None:
    home = create_home(tmp_path / "home")
    run = RunStore(home, clock=lambda: TS).create("R-0001")
    policy = Policy(
        load_rules(home.rules / "tools.yaml"),
        log=lambda d: run.log_event(**d.event_fields(), step="implement", attempt=1),
    )

    policy.classify("worker", call("run_command", argv=["env"]))

    [event] = run.events()
    assert event == Event(
        ts=TS,
        run_id="R-0001",
        kind="tool_decision",
        step="implement",
        attempt=1,
        outcome="deny",
        message=event.message,
    )
    assert "never" in event.message and "env" in event.message


def test_from_home_loads_the_home_rules(tmp_path: Path) -> None:
    home = create_home(tmp_path / "home")

    policy = Policy.from_home(home)

    assert policy.rules == load_rules(DEFAULT_RULES)
