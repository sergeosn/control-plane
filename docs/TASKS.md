# Control Plane v0: tasks

Tasks 1 to 16 build the system by hand until it can run a task on its own repository. From Task 17 on, the system delivers itself: each task becomes a file in the home repository and goes through `cplane run`.

Section numbers refer to `SPEC.md`.

## How to work through Phase 0

Take one task per coding-agent session. Give the agent `AGENTS.md`, the spec sections the task names, and the task itself. Read the diff yourself before committing. Commit as `[Task N] Title`, and later corrections as `[FIX] ...`.

Keep a file `docs/bootstrap-log.md`. Each time the agent gets something wrong, write one line: what it did, and what you changed in the instructions so it would not happen again. In Task 14 this log becomes the first version of the lane's procedure and the reviewer's rubric. You are doing by hand what the system will later do for itself.

**Done, for every task:** the acceptance checks below pass as automated tests; `ruff check` and `ruff format --check` are clean; coverage stays at or above 85%; no test touches the network or a model; the spec is updated if the implementation had to differ from it.

---

## Phase 0: bootstrap by hand

### [Task 1] Project skeleton and conventions

Spec: 17.

Set up a `uv` project on Python 3.12 or newer with the `src/control_plane` layout, `ruff`, `pytest` with `pytest-asyncio`, coverage configuration, and a GitHub Actions workflow that runs lint, format check and tests with the coverage threshold. Add `AGENTS.md`: the package layout, how to run the checks, and the standing rules (no network in tests, no new dependency without a stated reason, files first and index second).

Done when:

- CI is green on the empty package.
- `uv run cplane --version` prints the version.

### [Task 2] Domain models and file formats

Spec: 6, 8.4, 12.

Write the Pydantic models: `Task` with its front-matter parser, `Plan`, `WorkReport`, `Review` with the derived verdict, `Approval`, `Event`, `Manifest`. Export a JSON Schema for each artifact that is valid for a strict tool definition.

Done when:

- Every model round-trips through its file format.
- A malformed task file is rejected with an error that names the field.
- A table-driven test covers verdict derivation: any blocker or major finding fails, any unmet criterion fails, any failed rubric row fails, otherwise pass.
- A test asserts that every object in every exported schema has `additionalProperties: false`.

### [Task 3] Home repository and run store

Spec: 5.

Implement `cplane init`, which creates the home layout with the default lane and rules and makes the first commit. Every other command finds the home through `--home` or `CPLANE_HOME`. Implement the run folder writer: atomic JSON writes, append-only JSONL, the event log, a commit when the run finishes, and verification that finished run folders are unchanged.

Done when:

- `cplane init` produces the layout from section 5 as a git repository.
- A failure injected between the temporary write and the rename leaves no partial JSON file.
- Modifying a file in a finished run folder is reported by verification.

### [Task 4] Index

Spec: 14.

Add the SQLite index with SQLAlchemy 2.0 and the async engine: the six tables, a schema version, and `cplane reindex`.

Done when:

- Deleting the database and running `reindex` gives the same query results as before.
- A failing index write does not fail the file write that preceded it.
- A schema version mismatch triggers a rebuild.

### [Task 5] Model client protocol, scripted client, cost accounting

Spec: 8.2, 16.

Define the `LLMClient` protocol with request and response types that mirror the parts of the Messages API the loop uses: text and `tool_use` blocks, `stop_reason`, usage. Implement `ScriptedClient`. Implement cost calculation from the price table in `plane.yaml`, including cache read and cache write tokens.

Done when:

- `ScriptedClient` replays its script in order and raises a clear error when the script runs out.
- Cost matches expected values for a table of usage examples.
- A model missing from the price table is an error. It never silently costs zero.

### [Task 6] Tools and the executor

Spec: 9 (truncation), 10.2, 10.4.

Implement the tools with Pydantic input models: `read_file`, `list_dir`, `search`, `write_file`, `edit_file`, `delete_file`, `run_command`. Implement the path jail and the command runner.

Done when:

- A relative path with `..`, an absolute path outside the roots, and a symlink pointing outside are each denied.
- `edit_file` fails with an instructive error when the old text is missing or occurs more than once.
- A command that exceeds its timeout is killed together with its child processes.
- A subprocess started by `run_command` does not see the API key variable.
- Long file reads, search results and command output are truncated, and the result says how much was dropped.

### [Task 7] Policy: classification and decisions

Spec: 10.1, 10.3.

Implement the rules loader with validation, `classify(role, call)` returning the class, the action and the rule id, the per-role tool exposure check, protected-path detection, and logging of every decision as an event.

Done when:

- A table-driven test covers every rule in the default `tools.yaml`.
- A command no rule matches is parked. A `blocked` command is denied. Neither is executed.
- A role calling a tool it does not have is denied even when the call is well formed.
- A malformed rules file is rejected at load with the position of the error.

### [Task 8] The agent loop

Spec: 8.2 to 8.6.

Implement `ApiLoopExecutor` against the `LLMClient` protocol, tested with `ScriptedClient` only.

Done when:

- A scripted happy path ends in a validated artifact on disk.
- A response with several `tool_use` blocks produces one user message with one `tool_result` per block.
- An invalid submit payload is returned as an error with the validation messages, accepted after repair, and fails the step on the third invalid attempt.
- `end_turn` without a submit call is nudged once and fails the second time.
- Each budget (turns, tokens, cost, wall time) stops the step, and the 80% notice arrives inside a `tool_result`.
- Three identical calls in a row add a notice and five fail the step.
- `max_tokens`, `refusal` and `model_context_window_exceeded` each end the step with the reason recorded.
- `transcript.jsonl` and `state.json` are current after every model response and every tool result.

### [Task 9] Parking, approvals and resume

Spec: 12.

Implement the approval queue, parking on a `park` decision, `cplane approvals`, `approve`, `deny`, and resuming a step from its transcript.

Done when:

- A `write-external` call parks the run, creates an approval entry and exits the process.
- After approval the call executes exactly once and the loop continues.
- After denial the model receives an error result with the human's reason and the loop continues.
- A call marked started and not finished is not re-run on resume. It returns the interrupted error.
- A response with two calls, one allowed and one parked, resumes correctly with both results in one message.

### [Task 10] Anthropic client

Spec: 8.2, 8.3.

Read the current documentation for tool use, structured outputs and stop reasons before writing this task, because model ids and parameters change. Implement the real client with the async SDK: tool definitions with `strict: true`, mapping of content blocks, stop reasons and usage, retries, and conversion of a persistent rate limit into a park with `resume_after`. Credentials follow section 14: an API key in `CPLANE_ANTHROPIC_API_KEY`, or a gateway through `CPLANE_LLM_BASE_URL` and `CPLANE_LLM_AUTH_TOKEN`. If your gateway speaks only the OpenAI chat-completions format, implement that client in this task instead, behind the same protocol and with the same tests.

Done when:

- Unit tests with mocked HTTP cover the mapping of each stop reason and all four usage fields.
- A persistent 429 becomes a rate-limit park that carries the retry time.
- A live smoke test, marked `live`, enabled by `CPLANE_LIVE=1` and skipped in CI, completes a two-turn tool call against the real API.

### [Task 11] Workspace

Spec: 5, 13.

Implement worktree creation and removal, branch naming, diff export, commit, and the `merge_and_verify` action with revert on red.

Done when, on a fixture repository in a temporary directory:

- A worktree is created from the recorded base commit on branch `run/<run-id>`.
- `diff.patch` matches the changes made in the worktree.
- A merge with green tests lands on `main`.
- A merge with red tests is reverted and `main` is back at the pre-merge commit.
- A conflicting merge fails cleanly and leaves `main` untouched.

### [Task 12] Context builder and manifest

Spec: 9.

Implement `BRIEF.md` and `INDEX.md` generation with the per-role recipes, the repo map, the manifest with hashes, and `cplane inspect --context`.

Done when:

- The brief stays under its size budget for the largest fixture task.
- The index lists every provided file with a description and a line count.
- A test asserts that the reviewer's context contains no worker transcript.
- The repo map stays bounded on a fixture with thousands of files.
- Manifest hashes verify against the files, and a changed file is detected.
- `inspect --context` shows provided files against files read for a scripted run.

### [Task 13] Gates

Spec: 11.

Implement the gates and the `scope` check, with commands read from `project.yaml` in the home repository.

Done when:

- Each gate has a passing and a failing test on a fixture repository.
- `test_integrity` catches a deleted test, a reduced test count and a new skip marker.
- `guardrail_regression` runs the control copy's suite against the worktree's source.
- `scope` adds out-of-plan files to the reviewer's context and does not fail the run.

### [Task 14] Flow engine and the development lane

Spec: 7.

Implement the flow schema, validation and the state machine with persistence and resume. Write version 1 of the lane files from `docs/bootstrap-log.md`: `procedure.md`, `rubric.md` with row ids, and the three role prompts.

Done when:

- Validation rejects a flow with a missing target, an unreachable step, an unreachable `done`, an unknown role, and an uncovered outcome, each with its own test.
- A scripted run goes through plan, a failed gate, a second attempt, a failed review, a third attempt, a passed review, and parks at the merge approval.
- Exceeding `max_iterations` parks with a `budget` approval.
- A false `when` skips the step. A plan that touches a protected path forces the plan approval.
- A run can be resumed from inside every step kind.

### [Task 15] CLI, end-to-end test and selfcheck

Spec: 13, 14, 16.

Complete the CLI over the application layer, write the scripted end-to-end test, implement `cplane selfcheck`, and finish the guardrail regression suite.

Done when:

- Every command in section 14 works against a temporary home.
- The end-to-end test takes a task from backlog to merged on a fixture repository with scripted models.
- `cplane selfcheck` runs that scenario with no network access.
- Every case listed in section 16 exists in `tests/guardrails/`.

### [Task 16] First live run and cutover

Spec: 13.

Create the control checkout and the tag `last-known-good`. Implement `cplane self-update`. Write the task files for Phase 1 in the home repository. Run one small real task on the system's own repository with the real API. A good first one is the `--json` flag from the example in section 6.

Done when:

- One task went from backlog to merged with human input only at approvals.
- `cplane self-update` adopted the result.
- A rollback was rehearsed: the tag moved back and the engine ran from the previous version.
- The README records the run: iterations, tokens, cost, wall time, and what went wrong.

Tag this commit `v0.1`.

---

## Phase 1: delivered by the system itself

Start with Tasks 17 and 18. They are small, they touch nothing protected, and they produce the numbers needed to tune budgets. Do Task 24 before pointing the system at any repository you do not control. Tasks that touch `policy/` or `tests/guardrails/` will raise a plan approval, which is the design working.

The order below follows the build order in the Overview: self-tuning first, then the interface, then scheduling and goals.

### [Task 17] Run report

`cplane report` computes the metrics in section 16 from run folders.

Done when the report shows, for any set of runs, the share merged without human input beyond approvals, median iterations, cost and wall time, gate failures by gate, and tool calls by class and decision.

### [Task 18] Prompt caching

Cache the stable prefix (system prompt and tool definitions) and record cache read and write tokens.

Done when the report shows a cache read ratio, and a scripted test proves that the cached prefix is byte-identical between two steps of the same role.

### [Task 19] Knowledge proposals

Add a `retro` step after a passed review. It proposes facts, decisions, constraints and rubric rows as files in the run folder. An approval applies them to the project's knowledge and commits. A fact that contradicts an existing one moves the old one to the archive.

Done when a run ends with proposals in the queue, an approved proposal appears as a commit in the home repository, and a denied one changes nothing.

### [Task 20] Questions become knowledge

When a planner's question is answered, the answer is proposed as a fact. A question that matches an existing fact is reported as a context error.

Done when asking the same question in a second run is flagged, with a pointer to the fact that should have been found.

### [Task 21] Rule candidates

Each denial of a parked call creates a candidate rule. A call pattern approved five times with no denial becomes a candidate for automatic allow. Both go through approval.

Done when candidates appear in the queue with their evidence, and an accepted candidate is a commit to `rules/tools.yaml`.

### [Task 22] Replay client

An `LLMClient` that replays the transcripts of a finished run.

Done when a recorded real run can be re-executed in CI with identical tool calls and the same final state.

### [Task 23] Transcript compaction

When a step nears its token budget, older tool results are replaced by short summaries, and the full text stays in the transcript on disk.

Done when a scripted long step finishes within budget, and `inspect` shows what was compacted.

### [Task 24] Container sandbox for commands

`run_command` executes in a container with the worktree mounted and no network.

Done when a guardrail test proves that a command cannot read a file outside the worktree and cannot open a network connection.

### [Task 25] Second provider

Add a second implementation of `LLMClient` for another model family and use it for the reviewer.

Done when a task's `roles` can name profiles from two providers, and the manifest records both.

### [Task 26] CLI-agent executor

Add an `Executor` that runs a role as a headless CLI coding agent in the worktree, with a hook that sends each tool call to the same policy engine.

Done when the same task runs with either executor by changing its lane configuration, and a parked call resumes under both.

### [Task 27] Kernel and API

A persistent process owns the application layer and exposes it through an async FastAPI service with an event stream. The CLI becomes a client.

Done when every CLI command works through the API, and two clients see the same run events live.

### [Task 28] Web interface

A read-only run inspector and the approval queue with approve and deny.

Done when a parked run can be inspected and decided from the browser.

### [Task 29] Scheduler and parallel branches

The kernel takes tasks from the backlog by priority and deadline, limits concurrent model calls, and resumes parked runs by itself. Flows gain parallel branches.

Done when several tasks run concurrently within the limit, and a flow with two reviewers in parallel joins their results.

### [Task 30] Goals

A goal states what must become true and how to verify it. A planning lane breaks it into tasks, which go through a plan approval.

Done when an approved goal produces task files that run without further editing.

### [Task 31] Evals

Run a fixed set of tasks on a fixture repository under different configurations of model, flow and strictness. Code scores the results.

Done when two configurations can be compared on pass rate, iterations and cost.

### [Task 32] First external project

Register `price-change-detector` as a project with its own knowledge and check commands, and deliver the LLM extraction fallback through the system.

Done when that feature is merged there with human input only at approvals.
