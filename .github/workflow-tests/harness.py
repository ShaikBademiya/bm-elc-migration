"""Shared harness for testing deploy.yml one job at a time.

The point of this file is that tests execute the SCRIPT THAT IS IN THE WORKFLOW,
pulled out of the YAML at run time. A hand-copied duplicate of a `run:` block
would pass forever after someone edited the real one.

GitHub runs a `run:` step on Linux with `bash --noprofile --norc -e {0}` when no
`shell:` is given - no `-u`, no `-o pipefail`. That is replicated exactly, so a
step relying on the defaults behaves here the way it behaves on the runner.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml


def _find_repo() -> Path:
    """Locate the repo whose workflows are under test.

    These tests run from two places: inside the repo on a GitHub runner, and
    from a sibling directory on a developer machine. Rather than hardcode
    either, walk up for the workflow directory and fall back to the sibling
    clone. $DEPLOY_REPO overrides both.
    """
    override = os.environ.get("DEPLOY_REPO")
    if override:
        return Path(override).resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".github" / "workflows" / "deploy.yml").is_file():
            return parent

    sibling = here.parent.parent / "bm-elc-migration"
    if (sibling / ".github" / "workflows" / "deploy.yml").is_file():
        return sibling

    raise RuntimeError("cannot locate the repo containing .github/workflows/deploy.yml")


ROOT = Path(__file__).resolve().parent
REPO = _find_repo()
WORKFLOWS = REPO / ".github" / "workflows"


# --------------------------------------------------------------- yaml loading

class _Loader(yaml.SafeLoader):
    """PyYAML resolves the bare key `on:` to the boolean True (YAML 1.1)."""


def load_workflow(name: str) -> dict:
    with (WORKFLOWS / name).open(encoding="utf-8") as fh:
        doc = yaml.load(fh, Loader=_Loader)
    # Normalise the `on:`/True key so callers can just say wf["on"].
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


def job(workflow: str, job_id: str) -> dict:
    return load_workflow(workflow)["jobs"][job_id]


def step_script(workflow: str, job_id: str, step_index: int = 0) -> str:
    """The verbatim `run:` text of a step, straight out of the YAML."""
    step = job(workflow, job_id)["steps"][step_index]
    if "run" not in step:
        raise AssertionError(f"{workflow}:{job_id} step {step_index} has no `run:` block")
    return step["run"]


# ------------------------------------------------------------------- bash


def find_bash() -> str:
    """Locate a bash that executes. On Windows `which bash` can be the WSL stub."""
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        shutil.which("bash"),
        "/bin/bash",
    ):
        if not candidate or not Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-c", "echo ok"], capture_output=True, text=True, check=False
        )
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    raise RuntimeError("no working bash found")


BASH = find_bash()


@dataclass
class Result:
    rc: int
    stdout: str
    stderr: str
    outputs: dict[str, str]
    summary: str
    raw_output_file: str

    @property
    def combined(self) -> str:
        return self.stdout + self.stderr


def run_step(script: str, env: dict[str, str] | None = None, unset: tuple[str, ...] = ()) -> Result:
    """Execute a `run:` block the way the runner does, with the Actions files faked.

    Returns the exit code plus whatever the step wrote to $GITHUB_OUTPUT and
    $GITHUB_STEP_SUMMARY, because for most of these steps the file contents ARE
    the contract - the exit code alone says nothing about what downstream jobs
    will receive.
    """
    workdir = Path(tempfile.mkdtemp(prefix="ghstep_"))
    try:
        script_path = workdir / "step.sh"
        script_path.write_text(script, encoding="utf-8", newline="\n")
        out_file = workdir / "github_output"
        sum_file = workdir / "github_step_summary"
        out_file.touch()
        sum_file.touch()

        # A deliberately minimal environment: only what the runner guarantees,
        # so a step that silently depends on the developer's shell is caught.
        child = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", str(workdir)),
            "GITHUB_OUTPUT": str(out_file),
            "GITHUB_STEP_SUMMARY": str(sum_file),
            "GITHUB_ENV": str(workdir / "github_env"),
            "GITHUB_PATH": str(workdir / "github_path"),
        }
        child.update(env or {})
        for key in unset:
            child.pop(key, None)

        proc = subprocess.run(
            [BASH, "--noprofile", "--norc", "-e", str(script_path)],
            capture_output=True,
            text=True,
            env=child,
            cwd=workdir,
            check=False,
            timeout=60,
        )

        raw = out_file.read_text(encoding="utf-8")
        outputs: dict[str, str] = {}
        for line in raw.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value

        return Result(
            rc=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            outputs=outputs,
            summary=sum_file.read_text(encoding="utf-8"),
            raw_output_file=raw,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ----------------------------------------------------------------- reporting


class Suite:
    def __init__(self, title: str) -> None:
        self.title = title
        self.rows: list[tuple[str, str, bool, str]] = []

    def check(self, group: str, name: str, passed: bool, detail: str = "") -> bool:
        self.rows.append((group, name, bool(passed), detail))
        return bool(passed)

    def report(self) -> int:
        import sys

        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")

        print(f"\n{'=' * 78}\n{self.title}\n{'=' * 78}")
        current = None
        for group, name, passed, detail in self.rows:
            if group != current:
                print(f"\n-- {group}")
                current = group
            mark = "PASS" if passed else "FAIL"
            print(f"  [{mark}] {name}")
            if detail:
                print(f"         {detail}")

        failed = [row for row in self.rows if not row[2]]
        print(f"\n{'-' * 78}")
        print(f"{len(self.rows) - len(failed)}/{len(self.rows)} passed, {len(failed)} failed")

        self._write_job_summary(failed)
        return 1 if failed else 0

    def _write_job_summary(self, failed: list) -> None:
        """Put the result in the run's summary, not only in a collapsed log."""
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not path:
            return

        groups: dict[str, list[bool]] = {}
        for group, _name, passed, _detail in self.rows:
            groups.setdefault(group, []).append(passed)

        lines = [f"## {self.title}", ""]
        if failed:
            lines.append(f"**FAILED** - {len(failed)} of {len(self.rows)} assertions")
            lines.append("")
            lines.append("| Assertion | Detail |")
            lines.append("| --- | --- |")
            for group, name, _passed, detail in failed:
                lines.append(f"| {group}: {name} | `{detail}` |")
        else:
            lines.append(f"**PASSED** - all {len(self.rows)} assertions")
        lines += ["", "| Group | Passed |", "| --- | --- |"]
        for group, results in groups.items():
            lines.append(f"| {group} | {sum(results)}/{len(results)} |")

        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
