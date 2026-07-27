"""Static checks on the GCS sync targets in the deployment workflows.

Two failure modes here are invisible at runtime, because `gsutil rsync` reports
success either way:

  1. Writing outside a prefix Composer mounts. Composer only surfaces
     gs://<bucket>/dags and gs://<bucket>/data to workers, so anything at
     gs://<bucket>/dbt is deployed and then silently never read.
  2. A destructive `rsync -d` against a prefix this domain does not own. On a
     shared Composer bucket that deletes another domain's files.

Run without arguments to check every deployment workflow.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = [
    ROOT / ".github" / "workflows" / "deploy.yml",
    ROOT / ".github" / "workflows" / "release.yml",
]

# Matches a gsutil destination built from the COMPOSER_BUCKET secret, capturing
# whatever path follows it.
DESTINATION = re.compile(r"gs://\$\{\{\s*secrets\.COMPOSER_BUCKET\s*\}\}/(?P<path>\S*)")

MOUNTED_PREFIXES = ("dags/", "data/")

# The literal text a domain-namespaced destination must contain. Written in
# pieces so this file never contains a GitHub Actions expression that the
# runner would try to interpolate.
DOMAIN_REF = "${" + "{ env.DOMAIN }" + "}"


def check(path: Path) -> list[str]:
    if not path.is_file():
        return [f"{path.name}: missing"]

    problems: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    found = 0

    for number, line in enumerate(lines, 1):
        match = DESTINATION.search(line)
        if not match:
            continue

        found += 1
        target = match.group("path")

        if not target.startswith(MOUNTED_PREFIXES):
            problems.append(
                f"{path.name}:{number}: destination '{target}' is outside dags/ and data/; "
                "Composer will not mount it"
            )

        # Every destination must be domain-scoped, not only the destructive ones.
        # Deploys now go file-by-file via deploy_artifacts.sh, which deletes
        # removed files, so any un-namespaced destination can reach another
        # domain's objects on a shared bucket.
        if DOMAIN_REF not in line:
            problems.append(
                f"{path.name}:{number}: destination '{target}' is not domain-namespaced; "
                "on a shared bucket this can reach other domains' files"
            )

    # A silent zero-match would let this gate pass on a workflow whose
    # destinations had been restructured out from under it.
    if found == 0:
        problems.append(
            f"{path.name}: no COMPOSER_BUCKET destination found - the checker's pattern "
            "no longer matches this workflow and is not validating anything"
        )

    return problems


def main() -> int:
    problems: list[str] = []
    for workflow in WORKFLOWS:
        found = check(workflow)
        problems.extend(found)
        if not found:
            print(f"{workflow.name}: sync targets OK")

    for problem in problems:
        print(f"::error::{problem}")
        print(problem, file=sys.stderr)

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
