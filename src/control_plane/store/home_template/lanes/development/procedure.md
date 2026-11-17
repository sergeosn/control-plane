# Development lane: procedure

How development work is done in this lane. The planner, the worker and the reviewer each
read this file; the rubric (`rubric.md`) is how the result is graded.

## Planning

1. Read the task's goal and every acceptance criterion. Each criterion has an id; the plan,
   the work report and the review refer to criteria by id.
2. Read the project knowledge (`facts.md`, `constraints.md`, the decisions) and the repo map
   before proposing files to touch.
3. The plan names every file it expects to change in `files_to_touch`, and names for each
   acceptance criterion the test that will prove it.
4. Anything the task leaves open goes into `questions`. A plan with questions parks the run
   for an answer instead of guessing.

## Implementing

1. Write the test for each acceptance criterion before the code that satisfies it.
2. Keep the diff to the plan. A deviation from the plan is allowed when the code demands it,
   and it is written into the work report's `deviations`.
3. Run the project's check commands (format, lint, tests) before submitting the report.
4. Never delete or weaken an existing test to make the checks pass. A test that is wrong is
   an open issue in the report.
5. Do not add a dependency unless the task needs it, and say why in the report.
6. The work report claims, per criterion, `done` or `not_done` with evidence a reviewer can
   check: a test name, a file and line, a command and its output.

## Reviewing

1. Judge the diff, the gate results and the work report. The report is a set of unverified
   claims; the diff and the gate output are facts.
2. Grade every rubric row by id with `pass`, `fail` or `n/a` and give evidence.
3. State per acceptance criterion whether it is met, with evidence from the diff or tests.
4. Findings carry a severity. A `blocker` or `major` finding fails the review; the verdict is
   derived by the engine, not written by the reviewer.

## Iterating

A failed gate, a failed review or a denied approval sends the task back to implementation
with the gate output, the findings or the reason as files. Every return increments the
iteration counter; when it exceeds the budget the run parks for a human decision.
