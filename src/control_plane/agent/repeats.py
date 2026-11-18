"""Loop detection (docs/SPEC.md section 8.5).

The same tool with the same arguments three times in a row adds a notice to the
`tool_result`; five times fails the step. The tracker is a model so it can live in
`state.json` and survive a park or a crash.
"""

import json
from typing import Any

from pydantic import Field

from control_plane.domain.base import DomainModel

NOTICE_AT = 3
FAIL_AT = 5


class RepeatTracker(DomainModel):
    key: str | None = None
    count: int = Field(ge=0, default=0)

    def observe(self, tool: str, arguments: dict[str, Any]) -> int:
        """Record a call and return how many identical calls in a row this makes."""
        key = json.dumps([tool, arguments], sort_keys=True, default=str)
        self.count = self.count + 1 if key == self.key else 1
        self.key = key
        return self.count
