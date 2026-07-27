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

    # rsync invocations often wrap, so keep the destructive flag sticky until we
    # find the destination it applies to.
    destructive = False

    for number, line in enumerate(lines, 1):
        if "rsync" in line:
            destructive = bool(re.search(r"rsync\b.*(-r -d|-d\b|-rd\b)", line))

        match = DESTINATION.search(line)
        if not match:
            continue

        target = match.group("path")

        if not target.startswith(MOUNTED_PREFIXES):
            problems.append(
                f"{path.name}:{number}: destination '{target}' is outside dags/ and data/; "
                "Composer will not mount it"
            )

        if destructive and DOMAIN_REF not in line:
            problems.append(
                f"{path.name}:{number}: destructive rsync into '{target}' is not "
                "domain-namespaced; on a shared bucket this deletes other domains' files"
            )

        destructive = False

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
