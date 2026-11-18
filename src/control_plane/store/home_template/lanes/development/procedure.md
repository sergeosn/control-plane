# Development lane: procedure (version 1)

How a task goes from backlog to merge in this lane. The planner, the worker and the reviewer
each read this file; `rubric.md` is how the result is graded. Acceptance criteria carry ids,
and the plan, the work report and the review refer to them by id.

The flow: plan, an optional plan approval, implement, checks, review, merge approval, merge.
A failed check, a failed review or a denied approval sends the work back with the reason.

## Planning

1. Read the task's goal and every acceptance criterion, the project knowledge (facts,
   constraints, decisions) and the repo map before naming any file.
2. Plan only what the task asks. Name every file the work will change in `files_to_touch`;
   a directory or a glob is allowed when the files are not known yet.
3. Name, for each acceptance criterion, the test that will prove it.
4. Put what the task leaves open in `questions`. A plan with questions parks the run until a
   human answers; a guess does not. A plan that touches a protected path is approved by a
   human before any code is written.
5. When re-planning, the previous plan and the denial reason or the answers are in your
   context: address them.

## Implementing

1. Write the test for each acceptance criterion before the code that satisfies it.
2. Keep the change to the plan. A deviation is allowed when the code demands it, and it goes
   into the report's `deviations`.
3. Tests never touch the network or a real model. Use the project's scripted clients and
   fixtures.
4. Do not add a dependency unless the task needs it, and say in the report why the standard
   library or an existing dependency does not do the job.
5. If the implementation has to differ from the project's specification, update the
   specification in the same change.
6. Run the project's format, lint and test commands, with coverage, before submitting. The
   same commands run as gates after you, from configuration you cannot change.
7. Never delete, skip or weaken a test to make the checks pass. A test that is wrong is an
   open issue in the report.
8. Report each criterion as `done` or `not_done` with evidence a reviewer can check: a test
   name, a file and line, a command and its result.

## Reviewing

1. Judge the diff, the gate results and the plan. The work report is a set of unverified
   claims; the diff and the gate output are facts.
2. Grade every rubric row by its id with `pass`, `fail` or `n/a`, with evidence.
3. State for each acceptance criterion whether it is met, with evidence from the code or the
   tests.
4. Record each problem as a finding with a severity. A `blocker` or `major` finding, a failed
   row or an unmet criterion fails the review. The engine derives the verdict.
5. Files the plan did not name are listed in `scope.md`. Each needs a reason in the report's
   `deviations`.

## Iterating

The next attempt starts with a fresh conversation and gets, as files: the plan, its own
previous report, and the output of the failed gates, the review findings or the denial
reason. Read them first and fix what they name. Every return to implementation counts as an
iteration; past the task's budget the run parks for a human to extend it or end it.
