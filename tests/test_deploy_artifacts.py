"""Tests for the per-file artifact deploy script.

The script decides what is copied to and, more importantly, what is deleted
from a Composer bucket. A wrong answer here is a production data-loss event, so
the behaviour is pinned rather than trusted.

gsutil is replaced with a stand-in that records the operation instead of
performing it, so these run offline and touch no cloud resource.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "deploy_artifacts.sh"


def find_bash() -> str | None:
    """Locate a bash that can actually execute.

    On Windows `shutil.which("bash")` often resolves to the WSL launcher, which
    fails with execvpe(/bin/bash) when no distro is installed. Probe each
    candidate rather than trusting PATH order.
    """
    candidates = [
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "echo ok"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    return None


BASH = find_bash()

pytestmark = pytest.mark.skipif(BASH is None, reason="no working bash interpreter found")

FAKE_GSUTIL = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    args=("$@"); op=""
    for a in "${args[@]}"; do
      case "$a" in cp|rm|rsync) op="$a"; break;; esac
    done
    case "$op" in
      cp)    echo "CP ${args[-2]} -> ${args[-1]}" ;;
      rm)    echo "RM ${args[-1]}" ;;
      rsync) echo "RSYNC ${args[-2]} -> ${args[-1]}" ;;
    esac
    exit 0
    """
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A source tree plus a gsutil stand-in on PATH."""
    src = tmp_path / "artifacts" / "dags"
    src.mkdir(parents=True)
    (src / "existing.py").write_text("print('hi')\n", encoding="utf-8")
    (src / "second.py").write_text("print('hi')\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gsutil"
    fake.write_text(FAKE_GSUTIL, encoding="utf-8")
    fake.chmod(0o755)
    return tmp_path


def run(workspace: Path, manifest: str, mode: str = "incremental", **env_extra):
    env = dict(os.environ)
    env["PATH"] = f"{workspace / 'bin'}{os.pathsep}{env['PATH']}"
    env.update(env_extra)
    return subprocess.run(
        [BASH, str(SCRIPT), "artifacts/dags", "gs://bkt/dags/udp", mode],
        input=manifest,
        capture_output=True,
        text=True,
        cwd=workspace,
        env=env,
        check=False,
    )


def test_copies_modified_and_deletes_removed(workspace: Path) -> None:
    result = run(workspace, "M\tartifacts/dags/existing.py\nD\tartifacts/dags/gone.py\n")
    assert result.returncode == 0, result.stderr
    assert "CP artifacts/dags/existing.py -> gs://bkt/dags/udp/existing.py" in result.stdout
    assert "RM gs://bkt/dags/udp/gone.py" in result.stdout


def test_ignores_other_artifact_classes(workspace: Path) -> None:
    """A dbt or pyproject change must not be deployed as a DAG."""
    result = run(workspace, "M\tartifacts/dbt/model.sql\nM\tpyproject.toml\n")
    assert result.returncode == 0, result.stderr
    assert "CP " not in result.stdout
    assert "RM " not in result.stdout


def test_rename_removes_the_old_object(workspace: Path) -> None:
    """Without this, a renamed DAG would be left orphaned in the bucket."""
    result = run(workspace, "R100\tartifacts/dags/old.py\tartifacts/dags/existing.py\n")
    assert result.returncode == 0, result.stderr
    assert "RM gs://bkt/dags/udp/old.py" in result.stdout
    assert "CP artifacts/dags/existing.py -> gs://bkt/dags/udp/existing.py" in result.stdout


def test_skips_a_file_that_is_not_present_locally(workspace: Path) -> None:
    """Added-then-deleted within one range must not fail the deploy."""
    result = run(workspace, "A\tartifacts/dags/never_landed.py\n")
    assert result.returncode == 0, result.stderr
    assert "skip copy" in result.stdout


def test_mass_deletion_is_refused(workspace: Path) -> None:
    manifest = "".join(f"D\tartifacts/dags/f{n}.py\n" for n in range(30))
    result = run(workspace, manifest, MAX_DELETIONS="25")
    assert result.returncode == 1
    assert "refusing to delete 30 objects" in result.stdout
    assert "RM " not in result.stdout, "nothing may be deleted once the guard trips"


def test_full_mode_reconciles_without_deleting(workspace: Path) -> None:
    result = run(workspace, "", mode="full")
    assert result.returncode == 0, result.stderr
    assert "RSYNC artifacts/dags/ -> gs://bkt/dags/udp/" in result.stdout
    # -d would make a full reconciliation destructive; it must not appear.
    assert " -d " not in result.stdout


def test_full_mode_refuses_an_empty_source(workspace: Path) -> None:
    for f in (workspace / "artifacts" / "dags").iterdir():
        f.unlink()
    result = run(workspace, "", mode="full")
    assert result.returncode == 1
    assert "empty source" in result.stdout


def test_missing_source_directory_is_a_no_op(workspace: Path) -> None:
    shutil.rmtree(workspace / "artifacts" / "dags")
    result = run(workspace, "M\tartifacts/dags/existing.py\n")
    assert result.returncode == 0, result.stderr
    assert "nothing to deploy" in result.stdout
