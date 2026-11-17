"""Task 2: the engine derives the review verdict (SPEC.md section 8.4)."""

import pytest

from control_plane.domain import CriterionReview, Finding, Review, RubricRow


def row(result: str) -> RubricRow:
    return RubricRow(row_id="R", result=result, evidence="")  # type: ignore[arg-type]


def crit(met: bool) -> CriterionReview:
    return CriterionReview(id="AC", met=met, evidence="")


def finding(severity: str) -> Finding:
    return Finding(severity=severity, file="f.py", description="")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("rubric", "criteria", "findings", "verdict"),
    [
        ([], [], [], "pass"),
        ([row("pass")], [crit(True)], [], "pass"),
        ([row("pass"), row("n/a")], [crit(True), crit(True)], [finding("minor")], "pass"),
        ([row("pass")], [crit(True)], [finding("blocker")], "fail"),
        ([row("pass")], [crit(True)], [finding("major")], "fail"),
        ([row("pass")], [crit(True)], [finding("minor"), finding("major")], "fail"),
        ([row("pass")], [crit(True), crit(False)], [], "fail"),
        ([row("pass"), row("fail")], [crit(True)], [], "fail"),
        ([row("n/a")], [crit(False)], [], "fail"),
        ([row("fail")], [crit(False)], [finding("blocker")], "fail"),
    ],
    ids=[
        "empty-review-passes",
        "all-good-passes",
        "n/a-rows-and-minor-findings-pass",
        "blocker-fails",
        "major-fails",
        "major-among-minors-fails",
        "unmet-criterion-fails",
        "failed-rubric-row-fails",
        "unmet-criterion-with-n/a-row-fails",
        "everything-wrong-fails",
    ],
)
def test_verdict_derivation(
    rubric: list[RubricRow],
    criteria: list[CriterionReview],
    findings: list[Finding],
    verdict: str,
) -> None:
    review = Review(rubric=rubric, criteria=criteria, findings=findings)

    assert review.verdict == verdict


def test_verdict_is_not_a_field_and_not_serialised() -> None:
    review = Review(rubric=[], criteria=[], findings=[])

    assert "verdict" not in Review.model_fields
    assert "verdict" not in review.to_json()
