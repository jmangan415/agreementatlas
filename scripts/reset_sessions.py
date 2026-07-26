#!/usr/bin/env python3
"""Delete the ephemeral session workspaces left over from the pre-library server.

Local mode now stores agreements in `data/library/`, which persists. The old
`data/sessions/` directories are no longer read by anything, but they still hold
uploaded agreements and any enrichment that was run against them, so they are
removed deliberately here rather than silently at start-up.

    python scripts/reset_sessions.py            # asks before deleting
    python scripts/reset_sessions.py --yes      # no prompt
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "data" / "sessions"


def describe(directory: Path) -> str:
    sources = directory / "sources"
    files = (
        sorted(path.name for path in sources.iterdir() if path.is_file())
        if sources.is_dir()
        else []
    )
    enriched = (directory / "legal" / "lm_rules.jsonl").exists()
    if not files:
        return "empty"
    listed = ", ".join(files[:3]) + (
        f" +{len(files) - 3} more" if len(files) > 3 else ""
    )
    return f"{len(files)} document(s): {listed}" + (
        " [AI-enriched]" if enriched else ""
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = parser.parse_args()

    if not SESSIONS.is_dir():
        print(f"Nothing to do: {SESSIONS} does not exist.")
        return 0
    directories = sorted(path for path in SESSIONS.iterdir() if path.is_dir())
    if not directories:
        print(f"Nothing to do: {SESSIONS} is already empty.")
        return 0

    print(f"About to permanently delete {len(directories)} session workspace(s) from")
    print(f"  {SESSIONS}\n")
    for directory in directories:
        print(f"  {directory.name[:12]}…  {describe(directory)}")
    print("\nThis cannot be undone. Uploaded agreements in these workspaces are lost.")

    if not args.yes:
        answer = input("\nType 'delete' to confirm: ").strip().lower()
        if answer != "delete":
            print("Cancelled. Nothing was removed.")
            return 1

    for directory in directories:
        shutil.rmtree(directory, ignore_errors=True)
    print(f"\nRemoved {len(directories)} workspace(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
