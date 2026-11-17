"""The `cplane` command line.

Every command calls the application layer in `control_plane.app` (SPEC.md section 14).
Task 1 provides only the entry point and `--version`; commands arrive with later tasks.
"""

import argparse
import sys
from collections.abc import Sequence

from control_plane import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cplane", description="Control Plane")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
