# Development lane: rubric

The reviewer grades every row below by id with `pass`, `fail` or `n/a` and gives evidence.
Any failed row fails the review.

- [R1] Every acceptance criterion the report marks `done` is proven by a test or by evidence
  in the diff.
- [R2] The diff stays within the plan's `files_to_touch`, or the report explains each
  deviation.
- [R3] No existing test was deleted, skipped or weakened, and the number of tests did not
  fall.
- [R4] The change does what the task asks and nothing beyond it: no unrelated refactoring,
  no speculative features.
- [R5] Errors are handled where they can occur, and failure paths are covered by tests.
- [R6] Names, structure and documentation follow the project's conventions and the project
  knowledge files.
- [R7] No secret, credential or environment dump appears in the diff or in command output.
- [R8] The work report's `open_issues` and `deviations` are complete and honest about what
  was not done.
