"""Truncation helpers. Truncation is always announced (SPEC.md section 9).

Each truncated result says how much was dropped, so the model narrows its request instead
of assuming it saw everything.
"""

from collections.abc import Iterable


def numbered_lines(lines: Iterable[str], *, start: int) -> str:
    """`lines` prefixed with their 1-based numbers, counting from `start`."""
    return "\n".join(f"{number}\t{line}" for number, line in enumerate(lines, start))


def head_and_tail(text: str, *, limit: int) -> str:
    """`text` when it fits in `limit` characters, otherwise its head and tail with a notice."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    dropped = len(text) - limit
    notice = f"\n[... {dropped} characters dropped of {len(text)}; head and tail kept ...]\n"
    return text[:head] + notice + text[-tail:]
