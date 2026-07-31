"""Write a workflow step's `run:` script to a file so a runner can execute it.

Tests execute the script that is IN the workflow, read out of the YAML at run
time. A hand-copied duplicate of a `run:` block would keep passing forever after
someone edited the real one, which is the failure mode this whole exercise is
supposed to catch.

Usage: extract_step.py <workflow-file> <job-id> <step-index> <output-path>
"""

from __future__ import annotations

import sys
from pathlib import Path

from harness import step_script


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2

    workflow, job_id, index, destination = argv[0], argv[1], int(argv[2]), Path(argv[3])
    script = step_script(workflow, job_id, index)

    if not script.strip():
        print(f"::error::{workflow}:{job_id} step {index} has an empty run block", file=sys.stderr)
        return 1

    # newline="\n" so the file is LF even when this runs on Windows: a CRLF
    # script fails on a Linux runner with a bad-interpreter error.
    destination.write_text(script, encoding="utf-8", newline="\n")
    print(f"extracted {workflow}:{job_id} step {index} -> {destination} ({len(script)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
