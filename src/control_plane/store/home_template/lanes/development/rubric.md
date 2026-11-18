# Development lane: rubric (version 1)

The reviewer grades every row by its id with `pass`, `fail` or `n/a`, and gives evidence from
the diff, the tests or the gate output. Any failed row fails the review.

- [R1] Every acceptance criterion the report marks `done` is proven by a test that exercises
  it, or by evidence in the diff the reviewer has checked.
- [R2] The diff stays within the plan's `files_to_touch`; every file in `scope.md` has a
  reason in the report's `deviations`.
- [R3] No existing test was deleted, skipped or weakened, and no test reaches the network or
  a real model.
- [R4] The change does what the task asks and nothing beyond it: no unrelated refactoring, no
  speculative features.
- [R5] Errors are handled where they can occur, error messages say what went wrong and what
  to do, and the failure paths are covered by tests.
- [R6] No dependency was added, or the report says why the task needs it and why the standard
  library or an existing dependency does not do the job.
- [R7] The change follows the project knowledge (facts, constraints, decisions), its package
  layout and its naming conventions.
- [R8] Where the implementation differs from the project's specification, the specification
  is updated in the same change.
- [R9] No secret, credential or environment dump appears in the diff, and no check, policy,
  rule or tool configuration was weakened without the task asking for it.
- [R10] The report is honest: its claims match the diff and the gate output, and its
  `deviations` and `open_issues` name everything not done as planned.
