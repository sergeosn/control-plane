"""What each role is given (docs/SPEC.md section 9, the per-role table).

`always` are the sources every step of the role indexes. `required` and `optional` name the
step-specific inputs, which are fields of `StepInputs`: a step must be given every required
one and may be given only those its recipe lists. There is no input that carries a
transcript: the reviewer never sees the worker's conversation, and every attempt starts a
fresh transcript, so no role is given an earlier one.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Recipe:
    always: tuple[str, ...]
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


RECIPES: dict[str, Recipe] = {
    "planner": Recipe(
        always=("task", "knowledge", "procedure", "repo_map"),
        optional=("previous_plan", "denial_reason", "answers"),
    ),
    "worker": Recipe(
        always=("task", "knowledge", "procedure", "rubric", "repo_map"),
        required=("plan",),
        optional=("gate_outputs", "review", "work_report", "denial_reason"),
    ),
    "reviewer": Recipe(
        always=("task", "knowledge", "rubric", "repo_map"),
        required=("plan", "diff", "work_report"),
        optional=("gate_outputs", "out_of_plan"),
    ),
}
