"""The error a tool raises when it cannot do what was asked."""


class ToolError(Exception):
    """Carries a message that says what went wrong and what to try (SPEC.md section 8.2)."""
