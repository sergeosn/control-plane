"""Application service behind `cplane reindex` (SPEC.md sections 5 and 14)."""

from dataclasses import dataclass

from control_plane.store.home import Home
from control_plane.store.index import Index
from control_plane.store.runs import RunDiscrepancy, RunStore


@dataclass(frozen=True)
class ReindexReport:
    counts: dict[str, int]
    """Rows per table after the rebuild."""
    problems: list[str]
    """Files or records that could not be indexed, each with its path and the reason."""
    discrepancies: list[RunDiscrepancy]
    """Finished run folders that differ from what the home repository committed."""

    @property
    def ok(self) -> bool:
        return not self.problems and not self.discrepancies


async def reindex(home: Home) -> ReindexReport:
    """Delete and rebuild the index from the files, then verify finished run folders."""
    index = Index(home)
    try:
        problems = await index.rebuild()
        counts = await index.counts()
    finally:
        await index.close()
    return ReindexReport(counts, problems, RunStore(home).verify_finished())
