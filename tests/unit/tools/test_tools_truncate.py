"""Task 6: truncation is always announced (docs/SPEC.md section 9)."""

from control_plane.tools import head_and_tail, numbered_lines


def test_numbered_lines_prefix_each_line_with_its_number() -> None:
    assert numbered_lines(["a", "b"], start=1) == "1\ta\n2\tb"


def test_numbered_lines_start_at_the_given_line() -> None:
    assert numbered_lines(["x"], start=42) == "42\tx"


def test_head_and_tail_returns_short_text_unchanged() -> None:
    assert head_and_tail("short", limit=100) == "short"


def test_head_and_tail_keeps_both_ends_and_says_how_much_was_dropped() -> None:
    text = "".join(f"line {i:03d}\n" for i in range(100))

    out = head_and_tail(text, limit=200)

    assert out.startswith("line 000\n")
    assert out.endswith("line 099\n")
    assert len(out) < len(text)
    dropped = len(text) - 200
    assert f"{dropped} characters dropped" in out
    assert f"of {len(text)}" in out


def test_head_and_tail_at_the_limit_is_not_truncated() -> None:
    text = "x" * 50
    assert head_and_tail(text, limit=50) == text
