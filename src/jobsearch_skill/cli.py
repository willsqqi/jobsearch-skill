"""Minimal command-line interface for Jobsearch."""

from __future__ import annotations

import argparse
import json

from . import __version__


def main(argv: list[str] | None = None) -> int:
    """Run the Jobsearch command-line interface."""
    parser = argparse.ArgumentParser(prog="jobsearch")
    parser.add_argument("--version", action="store_true", help="Print the Jobsearch version.")
    args = parser.parse_args(argv)
    if args.version:
        print(json.dumps({"command": "version", "version": __version__}))
        return 0
    parser.print_help()
    return 0
