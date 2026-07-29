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
      case "$a" in cp|rm|rsync|ls) op="$a"; break;; esac
    done
    case "$op" in
      cp)    echo "CP ${args[-2]} -> ${args[-1]}" ;;
      rm)
        # FAKE_RM_FAIL lets a test drive the non-404 failure path, which used to
        # be reported as a successful removal.
        if [ -n "${FAKE_RM_FAIL:-}" ]; then
          echo "${FAKE_RM_FAIL}" >&2
          exit 1
        fi
        echo "RM ${args[-1]}"
        ;;
      rsync) echo "RSYNC ${args[*]}" ;;
      # Objects the destination is pretending to already hold, newline separated.
      ls)    [ -n "${FAKE_LS:-}" ] && printf '%s\\n' "${FAKE_LS}"; exit 0 ;;
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


def run(
    workspace: Path,
    manifest: str,
    mode: str = "incremental",
    src: str = "artifacts/dags",
    manifest_prefix: str | None = None,
    **env_extra,
):
    env = dict(os.environ)
    env["PATH"] = f"{workspace / 'bin'}{os.pathsep}{env['PATH']}"
    env.update(env_extra)
    argv = [BASH, str(SCRIPT), src, "gs://bkt/dags/udp", mode]
    if manifest_prefix is not None:
        argv.append(manifest_prefix)
    return subprocess.run(
        argv,
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


def test_full_mode_compares_checksums_and_reconciles(workspace: Path) -> None:
    """The regression that made full reconciliation a no-op for same-size edits.

    The artifact bundle is tarred with a frozen mtime so it is reproducible, and
    gsutil records that mtime on the object. rsync's default comparator is
    size+mtime, so with mtime pinned, any edit that preserves the file's size was
    invisible: the sync reported success and left the stale bytes in place.
    Verified live against a real bucket before this test was written.

    -d is what makes it a reconciliation rather than an upload: a file removed
    from the repo has to leave the bucket too.
    """
    result = run(workspace, "", mode="full")
    assert result.returncode == 0, result.stderr
    assert "artifacts/dags/ gs://bkt/dags/udp/" in result.stdout.replace("  ", " ")
    assert " -c " in f" {result.stdout} ", "checksum comparison must be forced"
    assert " -d " in f" {result.stdout} ", "reconciliation must remove deleted files"


def test_full_mode_refuses_a_mass_deletion(workspace: Path) -> None:
    """A wrong-but-not-empty bundle must not be allowed to empty a prefix."""
    ghosts = "\n".join(f"gs://bkt/dags/udp/ghost{n}.py" for n in range(30))
    result = run(workspace, "", mode="full", FAKE_LS=ghosts, MAX_DELETIONS="25")
    assert result.returncode == 1
    assert "would delete 30 object(s)" in result.stdout
    assert "RSYNC" not in result.stdout, "nothing may sync once the guard trips"


def test_full_mode_allows_a_deletion_under_the_cap(workspace: Path) -> None:
    ghosts = "\n".join(f"gs://bkt/dags/udp/ghost{n}.py" for n in range(3))
    result = run(workspace, "", mode="full", FAKE_LS=ghosts, MAX_DELETIONS="25")
    assert result.returncode == 0, result.stderr
    assert "will remove 3 object(s)" in result.stdout
    assert "RSYNC" in result.stdout


def test_a_real_delete_failure_fails_the_job(workspace: Path) -> None:
    """Previously every rm failure was logged as 'already absent' and went green.

    A permissions error left the object live in the environment while the deploy
    reported that it had removed it.
    """
    result = run(
        workspace,
        "D\tartifacts/dags/gone.py\n",
        FAKE_RM_FAIL="AccessDeniedException: 403 does not have storage.objects.delete",
    )
    assert result.returncode == 1
    assert "failed to delete gone.py" in result.stdout
    assert "already absent" not in result.stdout


def test_a_missing_object_is_still_not_an_error(workspace: Path) -> None:
    """The idempotent case must keep working - only genuine failures now fail."""
    result = run(
        workspace,
        "D\tartifacts/dags/gone.py\n",
        FAKE_RM_FAIL="CommandException: No URLs matched: gs://bkt/dags/udp/gone.py",
    )
    assert result.returncode == 0, result.stderr
    assert "already absent: gone.py" in result.stdout


def test_a_wildcard_path_is_refused(workspace: Path) -> None:
    """gsutil expands wildcards in rm, which would escape the deletion ceiling."""
    result = run(workspace, "D\tartifacts/dags/*.py\n")
    assert result.returncode == 1
    assert "would treat it as a wildcard" in result.stdout
    assert "RM " not in result.stdout


def test_a_copy_deploys_the_new_file_not_the_source(workspace: Path) -> None:
    """git reports `C<score> <source> <destination>`; the destination is the new file."""
    result = run(workspace, "C85\tartifacts/dags/second.py\tartifacts/dags/existing.py\n")
    assert result.returncode == 0, result.stderr
    assert "CP artifacts/dags/existing.py -> gs://bkt/dags/udp/existing.py" in result.stdout
    assert "second.py" not in result.stdout


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


def test_source_dir_may_differ_from_manifest_prefix(workspace: Path) -> None:
    """The regression that made every incremental deploy a silent no-op.

    Deploys read from an extracted bundle, so the files live at
    `_bundle/artifacts/dags` while `git diff` still reports
    `artifacts/dags/...`. When one value served both purposes nothing matched,
    zero files were copied, and the job reported success.
    """
    bundle = workspace / "_bundle" / "artifacts" / "dags"
    bundle.mkdir(parents=True)
    (bundle / "existing.py").write_text("print('from the bundle')\n", encoding="utf-8")

    result = run(
        workspace,
        "M\tartifacts/dags/existing.py\n",
        src="_bundle/artifacts/dags",
        manifest_prefix="artifacts/dags",
    )
    assert result.returncode == 0, result.stderr
    assert "1 file(s) to copy" in result.stdout
    assert "CP _bundle/artifacts/dags/existing.py -> gs://bkt/dags/udp/existing.py" in result.stdout


def test_manifest_prefix_defaults_to_the_source_dir(workspace: Path) -> None:
    """Callers where the two agree must keep working without passing a 4th arg."""
    result = run(workspace, "M\tartifacts/dags/existing.py\n")
    assert result.returncode == 0, result.stderr
    assert "CP artifacts/dags/existing.py -> gs://bkt/dags/udp/existing.py" in result.stdout
