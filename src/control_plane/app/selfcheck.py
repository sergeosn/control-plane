"""`cplane selfcheck`: a complete scripted flow against a fixture repository, with no network
(docs/SPEC.md sections 13 and 16).

The scenario is the one the end-to-end test drives through the command line. In a fresh
directory it builds a small project repository with its control checkout, a home, the
project with its gates and a task in the backlog. Scripted models then plan, implement with
a bug, so the `tests` gate fails and sends the work back, implement again, pass every gate,
and pass the review. The run parks at the merge approval; the selfcheck approves it, resumes
the run, and checks that the change is merged into `main` of the fixture project and that
the task, the run folder and the index say so.

The fixture's gates run the standard library's `unittest` with the running interpreter, so
the selfcheck needs nothing that a plain installation of the package lacks. Sockets are
blocked in-process for the whole scenario (`network_blocked`); the models are scripted, so
no step needs a credential.

`self-update` (Task 16) runs this before it moves `last-known-good`.
"""

import socket
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from control_plane.app import approvals as app_approvals
from control_plane.app import index as app_index
from control_plane.app import runs as app_runs
from control_plane.app import tasks as app_tasks
from control_plane.app.clients import ScriptedClients
from control_plane.app.home import init_home
from control_plane.app.projects import add_project
from control_plane.domain.project import CommandGate, GateSettings, GuardrailGate
from control_plane.flow import load_lane, read_flow_state
from control_plane.llm import Response, ScriptedClient, TextBlock, ToolUseBlock, Usage
from control_plane.policy import SUBMIT_TOOLS
from control_plane.store.git import Git
from control_plane.store.home import Home
from control_plane.store.plane import load_plane
from control_plane.store.runs import RunStore
from control_plane.workspace import branch_name, project_git

PROJECT = "selfcheck"
PROTECTED = ("tests/guardrails/",)
TASK_TITLE = "Add mul to calc"
TASK_GOAL = "`calc` can multiply two numbers."
TASK_CRITERIA = (
    "`calc.mul(a, b)` returns the product of `a` and `b`.",
    "A test in `tests/test_calc.py` proves it.",
)

CHECKS = (
    "set up a home, the fixture project and a task",
    "the run parks at the merge approval",
    "a failed gate sent the work back once",
    "the approved merge resumes to done",
    "main holds the merged change",
    "the task and the run folder are done",
    "the index rebuilds and the run folder matches its commit",
    "no network was touched",
)

UNITTEST = (sys.executable, "-m", "unittest", "discover", "-p", "test_*.py", "-s")

CALC = "def add(a, b):\n    return a + b\n"
CALC_BUGGY = CALC + "\n\ndef mul(a, b):\n    return a + b\n"
CALC_FIXED = CALC + "\n\ndef mul(a, b):\n    return a * b\n"
TEST_CALC = """import unittest

from calc import add


class TestCalc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)
"""
TEST_CALC_WITH_MUL = """import unittest

from calc import add, mul


class TestCalc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)

    def test_mul(self):
        self.assertEqual(mul(3, 4), 12)
"""
TEST_GUARD = """import unittest

from calc import add


class TestGuard(unittest.TestCase):
    def test_add_rejects_a_string(self):
        with self.assertRaises(TypeError):
            add("a", 1)
"""
FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n",
    "calc.py": CALC,
    "tests/test_calc.py": TEST_CALC,
    "tests/guardrails/test_guard.py": TEST_GUARD,
}


class NetworkBlockedError(RuntimeError):
    """Something tried to reach the network while the selfcheck forbids it."""


@contextmanager
def network_blocked() -> Iterator[None]:
    """Make any socket connection or name lookup in this process raise, then restore."""

    def deny(*args: object, **_kwargs: object) -> None:
        raise NetworkBlockedError(f"network access attempted during selfcheck: {args[1:]!r}")

    def deny_lookup(*args: object, **_kwargs: object) -> None:
        raise NetworkBlockedError(f"network access attempted during selfcheck: {args!r}")

    saved = (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo)
    socket.socket.connect = deny  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = deny  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = deny_lookup  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = saved  # type: ignore[method-assign]


# --- the fixture ----------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureProject:
    repo: Path
    control: Path
    gates: GateSettings


def build_fixture_project(root: Path) -> FixtureProject:
    """The fixture repository at `root/project` on `main`, and its control checkout next to it."""
    repo = root / "project"
    git = Git(repo)
    git.init()
    for name, text in FILES.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git.add(".")
    git.commit("Initial commit")
    control = root / "project-control"
    git.run("worktree", "add", "--quiet", "--detach", str(control), git.head())
    gates = GateSettings(
        tests=CommandGate(argv=[*UNITTEST, "tests"], timeout_s=300),
        guardrail_regression=GuardrailGate(
            argv=[*UNITTEST, "tests/guardrails"], source=".", timeout_s=300
        ),
    )
    return FixtureProject(repo=repo, control=control, gates=gates)


@dataclass(frozen=True)
class Scenario:
    home: Home
    repo: Path
    control: Path
    task_id: str


async def prepare(workdir: Path) -> Scenario:
    """A home in `workdir` with the fixture project registered and the task in the backlog."""
    fixture = build_fixture_project(workdir)
    home = init_home(workdir / "home")
    add_project(home, PROJECT, fixture.repo, protected=PROTECTED, gates=fixture.gates)
    entry = await app_tasks.new_task(
        home, title=TASK_TITLE, project=PROJECT, goal=TASK_GOAL, criteria=TASK_CRITERIA
    )
    return Scenario(home=home, repo=fixture.repo, control=fixture.control, task_id=entry.task.id)


# --- the scripted models ------------------------------------------------------------------


def _models(home: Home) -> dict[str, str]:
    plane = load_plane(home)
    roles = plane.defaults.roles.model_dump()
    return {role: plane.profiles[profile].model for role, profile in roles.items()}


def _turn(model: str, *blocks: ToolUseBlock | TextBlock, stop: str = "tool_use") -> Response:
    usage = Usage(input_tokens=1200, output_tokens=300)
    return Response(model=model, content=list(blocks), stop_reason=stop, usage=usage)  # type: ignore[arg-type]


def _use(tool_id: str, name: str, **arguments: Any) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name=name, input=arguments)


def _submit(role: str, tool_id: str, payload: dict[str, Any]) -> ToolUseBlock:
    return _use(tool_id, SUBMIT_TOOLS[role], **payload)


def _criteria(evidence: str) -> list[dict[str, str]]:
    return [
        {"id": f"AC{n}", "status": "done", "evidence": evidence}
        for n in range(1, len(TASK_CRITERIA) + 1)
    ]


def scripted_clients(home: Home) -> ScriptedClients:
    """The planner, worker and reviewer of the scenario, one scripted client each."""
    models = _models(home)
    rows = load_lane(home.lanes / "development").rubric_rows
    files = ["calc.py", "tests/test_calc.py"]
    plan = {
        "summary": "Add `mul` to calc.py with a unittest for it.",
        "steps": [{"id": "1", "description": "Write test_mul, then mul.", "files": files}],
        "files_to_touch": files,
        "tests": [
            {"criterion": "AC1", "description": "test_mul checks mul(3, 4) == 12"},
            {"criterion": "AC2", "description": "test_mul lives in tests/test_calc.py"},
        ],
        "risks": [],
        "out_of_scope": [],
        "questions": [],
    }
    report = {
        "summary": "Added mul and test_mul.",
        "criteria": _criteria("tests/test_calc.py::TestCalc::test_mul"),
        "deviations": [],
        "open_issues": [],
    }
    review = {
        "rubric": [{"row_id": row, "result": "pass", "evidence": "diff.patch"} for row in rows],
        "criteria": [
            {"id": f"AC{n}", "met": True, "evidence": "test_mul in diff.patch, tests gate passes"}
            for n in range(1, len(TASK_CRITERIA) + 1)
        ],
        "findings": [],
    }
    write = "write_file"
    planner, worker, reviewer = models["planner"], models["worker"], models["reviewer"]
    return ScriptedClients(
        {
            "planner": [
                _turn(planner, _use("toolu_p1", "read_file", path="calc.py")),
                _turn(planner, _submit("planner", "toolu_p2", plan)),
            ],
            "worker": [
                _turn(
                    worker,
                    _use("toolu_w1", write, path="calc.py", content=CALC_BUGGY),
                    _use("toolu_w2", write, path="tests/test_calc.py", content=TEST_CALC_WITH_MUL),
                ),
                _turn(worker, _submit("worker", "toolu_w3", report)),
                _turn(worker, _use("toolu_w4", write, path="calc.py", content=CALC_FIXED)),
                _turn(worker, _submit("worker", "toolu_w5", report)),
            ],
            "reviewer": [_turn(reviewer, _submit("reviewer", "toolu_r1", review))],
        }
    )


def text_only_client(home: Home, role: str) -> ScriptedClient:
    """A client that never calls its submit tool, so its step fails with `no_submit`."""
    model = _models(home)[role]
    text = TextBlock(text="I think I am done.")
    return ScriptedClient(
        [_turn(model, text, stop="end_turn"), _turn(model, text, stop="end_turn")]
    )


# --- the selfcheck ------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class SelfcheckReport:
    workdir: str
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[str]:
        return [f"{check.name}: {check.detail}" for check in self.checks if not check.ok]

    @property
    def ok(self) -> bool:
        return len(self.checks) == len(CHECKS) and not self.failures


class _CheckFailedError(Exception):
    pass


def _expect(condition: bool, detail: str) -> None:
    if not condition:
        raise _CheckFailedError(detail)


async def run_selfcheck(workdir: Path | None = None) -> SelfcheckReport:
    """Run the scenario in `workdir`, or in a temporary directory removed afterwards."""
    if workdir is not None:
        return await _run(workdir)
    with tempfile.TemporaryDirectory(prefix="cplane-selfcheck-") as scratch:
        return await _run(Path(scratch))


async def _run(workdir: Path) -> SelfcheckReport:
    report = SelfcheckReport(workdir=str(workdir))
    state: dict[str, Any] = {}
    steps = (_setup, _parks, _iterated, _merged, _main, _done, _reindexed, _offline)
    with network_blocked():
        for name, step in zip(CHECKS, steps, strict=True):
            try:
                detail = await step(workdir, state)
            except Exception as exc:  # noqa: BLE001 - a selfcheck reports, it does not raise
                report.checks.append(Check(name, False, f"{type(exc).__name__}: {exc}"))
                break
            report.checks.append(Check(name, True, detail))
    return report


async def _setup(workdir: Path, state: dict[str, Any]) -> str:
    scenario = await prepare(workdir)
    state["scenario"] = scenario
    state["clients"] = scripted_clients(scenario.home)
    return f"home {scenario.home.path}, task {scenario.task_id}"


async def _parks(workdir: Path, state: dict[str, Any]) -> str:
    scenario: Scenario = state["scenario"]
    outcome = await app_runs.run_task(scenario.home, scenario.task_id, clients=state["clients"])
    state["outcome"] = outcome
    _expect(
        (outcome.status, outcome.step) == ("parked", "merge_gate"),
        f"run {outcome.run_id} is {outcome.status} at {outcome.step}: {outcome.message}",
    )
    return f"run {outcome.run_id} waits for {outcome.approval_id}"


async def _iterated(workdir: Path, state: dict[str, Any]) -> str:
    scenario: Scenario = state["scenario"]
    run = RunStore(scenario.home).open(state["outcome"].run_id)
    iteration = read_flow_state(run).iteration
    outcomes = [(run.path / "gates" / str(n) / "results.json").read_text() for n in (1, 2)]
    _expect(iteration == 2, f"expected 2 iterations, found {iteration}")
    _expect('"outcome": "fail"' in outcomes[0], "the first gate run did not fail")
    _expect('"outcome": "pass"' in outcomes[1], "the second gate run did not pass")
    return "tests gate failed on attempt 1 and passed on attempt 2"


async def _merged(workdir: Path, state: dict[str, Any]) -> str:
    scenario: Scenario = state["scenario"]
    outcome = state["outcome"]
    await app_approvals.approve(scenario.home, outcome.approval_id, note="selfcheck")
    resumed = await app_runs.resume_run(scenario.home, outcome.run_id, clients=state["clients"])
    _expect(resumed.status == "done", f"run is {resumed.status}: {resumed.message}")
    return f"run {outcome.run_id} done"


async def _main(workdir: Path, state: dict[str, Any]) -> str:
    scenario: Scenario = state["scenario"]
    run_id = state["outcome"].run_id
    subject = project_git(scenario.repo).log_subjects()[0]
    _expect(subject == f"Merge {branch_name(run_id)}", f"main's last commit is {subject!r}")
    _expect("return a * b" in (scenario.repo / "calc.py").read_text(), "calc.py lacks mul")
    return subject


async def _done(workdir: Path, state: dict[str, Any]) -> str:
    scenario: Scenario = state["scenario"]
    run = RunStore(scenario.home).open(state["outcome"].run_id)
    status = app_tasks.find_task(scenario.home, scenario.task_id).task.status
    _expect(status == "done", f"task {scenario.task_id} is {status}")
    _expect(run.finished, f"run {run.run_id} has no result.json")
    return f"task {scenario.task_id} done"


async def _reindexed(workdir: Path, state: dict[str, Any]) -> str:
    scenario: Scenario = state["scenario"]
    reindexed = await app_index.reindex(scenario.home)
    _expect(reindexed.ok, f"problems {reindexed.problems}, {reindexed.discrepancies}")
    return ", ".join(f"{table}: {count}" for table, count in reindexed.counts.items())


async def _offline(workdir: Path, state: dict[str, Any]) -> str:
    return "sockets blocked in-process for the whole scenario; models scripted"
