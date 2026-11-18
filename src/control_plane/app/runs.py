"""The run service behind `cplane run` and `cplane resume` (docs/SPEC.md sections 7, 13, 14).

`run_task` takes a task from the backlog: it reads the lane, the project, `plane.yaml` and the
rules, builds one model client per role, and only then creates the run folder, the worktree
on `run/<run-id>` from `main` and the manifest. The flow engine runs in the foreground until
the run parks or ends. `resume_run` continues a parked or interrupted run in a new process.

`RunPorts` is what the engine asks of the application layer (`flow.StepPorts`): the context
of an agent step and its executor, the gates, and the `merge_and_verify` action. Before the
merge, the engine commits what the worker left in the worktree to the run branch, with its
own identity, so that the merge takes exactly the change that was reviewed and approved.

The task file's `status` follows the run: `running` when it starts, then `parked`, `done` or
`failed`. A finished run's task status is committed to the home repository on its own. The
index is refreshed after every run or resume; a failure there is logged, never raised.
"""

import asyncio
import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from control_plane.agent import ApiLoopExecutor, StepBudget, StepContext
from control_plane.app.clients import ClientFactory, RunError
from control_plane.app.context import (
    CONTEXT_DIR,
    prepare_step_context,
    read_manifest,
    write_manifest,
)
from control_plane.app.gates import run_checks
from control_plane.app.tasks import TaskEntry, TaskError, commit_status, find_task, set_status
from control_plane.context.builder import BRIEF_FILE
from control_plane.context.manifest import ManifestError, start_manifest
from control_plane.domain.approval import ApprovalType
from control_plane.domain.gates import GateReport
from control_plane.domain.manifest import RoleModel, RunBudget
from control_plane.domain.plane import ModelProfile, PlaneConfig
from control_plane.domain.project import CommandGate, ProjectConfig
from control_plane.domain.task import TaskStatus
from control_plane.flow import (
    ActionRequest,
    ActionResult,
    AgentRequest,
    FlowEngine,
    FlowReport,
    FlowValidationError,
    GateRequest,
    Lane,
    LaneError,
    PreparedAgent,
    load_lane,
)
from control_plane.llm import LLMClient, PriceTable
from control_plane.policy import TOOL_NAMES, PathJail, Policy, ProtectedPaths, load_rules
from control_plane.policy.roles import tools_for
from control_plane.policy.rules import RulesError
from control_plane.store.git import Git
from control_plane.store.home import Home
from control_plane.store.index import Index
from control_plane.store.plane import PlaneConfigError, load_plane, price_table
from control_plane.store.projects import ProjectConfigError, load_project
from control_plane.store.runs import RunFolder, RunStore, new_run_id
from control_plane.tools import ToolExecutor
from control_plane.workspace import (
    MAIN_BRANCH,
    Worktree,
    changed_files,
    commit,
    create_worktree,
    diff_bytes,
    merge_and_verify,
    open_worktree,
    project_git,
)

__all__ = [
    "ResumeResult",
    "RunError",
    "RunOutcome",
    "RunPorts",
    "current_engine_commit",
    "merge_test_command",
    "resume_run",
    "resume_runs",
    "run_task",
]

ROLES = ("planner", "worker", "reviewer")
RULES_FILE = "tools.yaml"
MERGE_OUTPUT = "merge/tests.txt"
STARTABLE: frozenset[str] = frozenset({"backlog", "failed"})
_SHA = re.compile(r"^[0-9a-f]{40}$")

Clock = Callable[[], datetime]


def _wall_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    task_id: str
    status: Literal["parked", "done", "failed"]
    step: str
    message: str
    approval_id: str | None = None
    approval_type: ApprovalType | None = None
    """The type of the entry `approval_id`, which says how a human decides it."""
    reason: str | None = None
    """Why the run parked without an approval (`rate_limit`, `connection`, `server_error`,
    `auth`); else None."""
    resume_after: datetime | None = None
    """The earliest time `cplane resume` tries the model again; None for `auth`."""


@dataclass(frozen=True)
class ResumeResult:
    """One run of `resume_runs`: its outcome, or why it could not be resumed."""

    run_id: str
    outcome: RunOutcome | None
    error: str | None = None


# --- what a run is built from ------------------------------------------------------------


@dataclass(frozen=True)
class _Session:
    home: Home
    entry: TaskEntry
    plane: PlaneConfig
    project: ProjectConfig
    lane: Lane
    profiles: dict[str, tuple[str, ModelProfile]]
    clients: dict[str, LLMClient]
    policy: Policy
    prices: PriceTable
    clock: Clock

    @classmethod
    def build(
        cls, home: Home, entry: TaskEntry, factory: ClientFactory, clock: Clock
    ) -> "_Session":
        """Read and check everything a run needs; nothing is written. Raises `RunError`, or
        the factory's own error (such as `CredentialsError`) for a client it cannot build."""
        task = entry.task
        try:
            plane = load_plane(home)
            project = load_project(home, task.project)
            lane = load_lane(home.lanes / task.lane)
            policy = Policy(load_rules(home.rules / RULES_FILE))
        except (PlaneConfigError, ProjectConfigError, LaneError, FlowValidationError) as exc:
            raise RunError(f"task {task.id}: {exc}") from exc
        except (RulesError, OSError) as exc:
            raise RunError(f"task {task.id}: rules/{RULES_FILE}: {exc}") from exc
        profiles: dict[str, tuple[str, ModelProfile]] = {}
        for role in ROLES:
            name = getattr(task.roles, role)
            if name not in plane.profiles:
                raise RunError(
                    f"task {task.id}: the {role} uses profile {name!r}, which plane.yaml "
                    f"does not define; profiles: {', '.join(sorted(plane.profiles))}"
                )
            profiles[role] = (name, plane.profiles[name])
        clients = {role: factory(role, profile) for role, (_, profile) in profiles.items()}
        return cls(
            home=home,
            entry=entry,
            plane=plane,
            project=project,
            lane=lane,
            profiles=profiles,
            clients=clients,
            policy=policy,
            prices=price_table(plane),
            clock=clock,
        )

    def role_models(self) -> list[RoleModel]:
        return [
            RoleModel(
                role=role,
                profile=name,
                model=profile.model,
                max_tokens=profile.max_tokens,
                temperature=profile.temperature,
            )
            for role, (name, profile) in self.profiles.items()
        ]

    def engine(self, tree: Worktree, run: RunFolder) -> FlowEngine:
        return FlowEngine(
            self.lane.flow,
            task=self.entry.task,
            ports=RunPorts(self, tree, run),
            protected=ProtectedPaths(self.project.protected_paths),
            clock=self.clock,
        )


# --- the ports the flow engine runs through ------------------------------------------------


def merge_test_command(gate: CommandGate | None) -> tuple[list[str], float]:
    """The command `merge_and_verify` runs on `main`: the project's `tests` gate."""
    if gate is None:
        raise RunError("project.yaml has no `tests` gate; the merge cannot be verified without one")
    return list(gate.argv), gate.timeout_s


class RunPorts:
    """`flow.StepPorts` over the context builder, the gates, the executor and the merge."""

    def __init__(self, session: _Session, tree: Worktree, run: RunFolder) -> None:
        self.session = session
        self.tree = tree
        self.run = run

    async def prepare_agent(self, request: AgentRequest) -> PreparedAgent:
        session = self.session
        role = request.role
        context_dir = request.run.path / CONTEXT_DIR / request.folder
        brief_file = context_dir / BRIEF_FILE
        if brief_file.is_file():
            # A step resumed in a new process: its context was written once already.
            brief = brief_file.read_text(encoding="utf-8")
        else:
            inputs = request.inputs
            if role == "reviewer":
                inputs = dataclasses.replace(
                    inputs, diff=await asyncio.to_thread(diff_bytes, self.tree)
                )
            prepared = prepare_step_context(
                session.home,
                request.run,
                step=request.folder,
                role=role,
                task_file=session.entry.path,
                worktree=self.tree.path,
                inputs=inputs,
            )
            brief = prepared.brief
        jail = PathJail(self.tree.path, writable=role == "worker", read_roots=[context_dir])
        tools = ToolExecutor(jail, tools=[t for t in tools_for(role) if t in TOOL_NAMES])
        executor = ApiLoopExecutor(
            client=session.clients[role],
            policy=session.policy,
            tools=tools,
            prices=session.prices,
            clock=session.clock,
        )
        _, profile = session.profiles[role]
        context = StepContext(
            run=request.run,
            seq=request.seq,
            step=request.step,
            attempt=request.attempt,
            role=role,
            system=session.lane.role_prompt(role).read_text(encoding="utf-8"),
            brief=brief,
            budget=StepBudget(**session.plane.defaults.step_budget.model_dump()),
            max_tokens=profile.max_tokens,
        )
        return PreparedAgent(context=context, executor=executor)

    async def run_gates(self, request: GateRequest) -> GateReport:
        if request.plan is None:
            raise RunError(f"run {request.run.run_id}: the checks need a plan and there is none")
        checks = await run_checks(
            request.run,
            attempt=request.attempt,
            tree=self.tree,
            project=self.session.project,
            gates=request.gates,
            plan=request.plan,
        )
        return checks.report

    async def run_action(self, request: ActionRequest) -> ActionResult:
        if request.action != "merge_and_verify":
            return ActionResult("error", f"unknown action {request.action!r}")
        try:
            argv, timeout = merge_test_command(self.session.project.gates.tests)
        except RunError as exc:
            return ActionResult("error", str(exc))
        task = self.session.entry.task
        await asyncio.to_thread(commit, self.tree, f"{task.id}: {task.title}")
        result = await merge_and_verify(self.tree, argv, timeout=timeout)
        if result.test_output:
            request.run.write_bytes(MERGE_OUTPUT, result.test_output.encode("utf-8"))
        return ActionResult(result.outcome, result.message)

    def changed_files(self, run: RunFolder) -> list[str]:
        paths: list[str] = []
        for change in changed_files(self.tree):
            paths += [p for p in (change.path, change.old_path) if p]
        return paths


# --- the service -------------------------------------------------------------------------


def current_engine_commit() -> str:
    """The commit of the checkout the engine runs from; zeros when it is not in git."""
    try:
        probe = Git(Path(__file__).resolve().parent).process("rev-parse", "HEAD")
    except OSError:
        return "0" * 40
    sha = probe.stdout.strip()
    return sha if probe.returncode == 0 and _SHA.fullmatch(sha) else "0" * 40


async def run_task(
    home: Home,
    task_id: str,
    *,
    clients: ClientFactory,
    clock: Clock = _wall_clock,
    engine_commit: str | None = None,
) -> RunOutcome:
    """Start a run of `task_id` and run it until it parks or ends."""
    entry = _find(home, task_id)
    status = entry.task.status
    if status not in STARTABLE:
        hint = (
            "its run continues with `cplane resume`"
            if status in ("running", "parked")
            else "it was merged"
        )
        raise RunError(f"task {task_id} is {status}; {hint}")
    session = _Session.build(home, entry, clients, clock)

    run_id = new_run_id(clock())
    entry = set_status(home, task_id, "running")
    try:
        base = project_git(session.project.repo).run("rev-parse", f"refs/heads/{MAIN_BRANCH}")
        run = RunStore(home, clock=clock).create(run_id)
        tree = create_worktree(session.project.repo, home.worktrees, run_id, base.strip())
        manifest = start_manifest(
            home=home.path,
            run_id=run_id,
            task_file=entry.path,
            project_base_commit=tree.base_commit,
            engine_commit=engine_commit or current_engine_commit(),
            models=session.role_models(),
            budget=RunBudget(
                max_iterations=entry.task.budget.max_iterations,
                max_cost_usd=entry.task.budget.max_cost_usd,
                max_wall_minutes=session.lane.flow.limits.max_wall_minutes,
            ),
            created_at=clock(),
        )
        write_manifest(run, manifest)
    except Exception:
        set_status(home, task_id, status)
        raise
    session = dataclasses.replace(session, entry=entry)
    try:
        report = await session.engine(tree, run).start(run)
    finally:
        await _sync_index(home, run_id)
    return await _settle(home, session.entry, run_id, report)


async def resume_run(
    home: Home, run_id: str, *, clients: ClientFactory, clock: Clock = _wall_clock
) -> RunOutcome:
    """Continue a parked or interrupted run. A run still waiting on an undecided approval
    changes nothing and is reported as parked."""
    try:
        run = RunStore(home, clock=clock).open(run_id)
    except FileNotFoundError:
        raise RunError(f"no run {run_id} in {home.path}") from None
    if run.finished:
        raise RunError(f"run {run_id} is finished")
    try:
        manifest = read_manifest(run)
    except (ManifestError, ValueError) as exc:
        raise RunError(f"run {run_id}: {exc}") from exc
    entry = _find(home, manifest.task_id)
    session = _Session.build(home, entry, clients, clock)
    tree = open_worktree(session.project.repo, home.worktrees, run_id, manifest.project_base_commit)
    try:
        report = await session.engine(tree, run).resume(run)
    finally:
        await _sync_index(home, run_id)
    return await _settle(home, entry, run_id, report)


async def resume_runs(
    home: Home, *, clients: ClientFactory, clock: Clock = _wall_clock
) -> list[ResumeResult]:
    """Resume every unfinished run, oldest first. A run that cannot be resumed is reported
    and does not stop the others."""
    store = RunStore(home, clock=clock)
    results = []
    for run_id in store.list_ids():
        if store.open(run_id).finished:
            continue
        try:
            outcome = await resume_run(home, run_id, clients=clients, clock=clock)
        except Exception as exc:  # noqa: BLE001 - one run's failure must not stop the others
            results.append(ResumeResult(run_id, None, f"{type(exc).__name__}: {exc}"))
        else:
            results.append(ResumeResult(run_id, outcome))
    return results


def _find(home: Home, task_id: str) -> TaskEntry:
    try:
        return find_task(home, task_id)
    except TaskError as exc:
        raise RunError(str(exc)) from exc


async def _settle(home: Home, entry: TaskEntry, run_id: str, report: FlowReport) -> RunOutcome:
    status: TaskStatus = report.status
    current = find_task(home, entry.task.id)
    if current.task.status != status:
        current = set_status(home, entry.task.id, status)
    if report.status in ("done", "failed"):
        commit_status(home, current)
    await _sync_index(home, run_id)
    return RunOutcome(
        run_id=run_id,
        task_id=entry.task.id,
        status=report.status,
        step=report.step,
        message=report.message,
        approval_id=report.approval_id,
        approval_type=_approval_type(home, run_id, report.approval_id),
        reason=report.reason,
        resume_after=report.resume_after,
    )


def _approval_type(home: Home, run_id: str, entry_id: str | None) -> ApprovalType | None:
    if entry_id is None:
        return None
    types = {a.id: a.type for a in RunStore(home).open(run_id).approvals()}
    return types.get(entry_id)


async def _sync_index(home: Home, run_id: str) -> None:
    index = Index(home)

    async def write() -> None:
        try:
            await index.open()
            await index.sync_tasks()
            await index.sync_run(run_id)
        finally:
            await index.close()

    await index.attempt(write(), f"run {run_id}")
