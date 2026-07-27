"""Artifact validation gate for pull requests.

Implements the three validation stages TRD-002 specifies for `validate-dags.yml`:

  1. Schema validation   - YAML/Python/SQL parse, plus a credential scan
  2. DAG structure check - every file under artifacts/dags must be importable and
                           should declare a DAG
  3. Coverage report     - cross-reference configs against DAGs, both directions

TRD-002 expects these to be `amf.cli validate` once the framework lands in this
repo. Until then this script is the stand-in: same gates, same pass/fail
semantics, so the workflow around it does not have to change later.

Errors fail the build. Warnings are reported and do not.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts"

# Patterns that must never appear in a deployable artifact. Deliberately narrow:
# a noisy credential scanner gets disabled, which is worse than a narrow one.
CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GCP service account key", re.compile(r'"type"\s*:\s*"service_account"')),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Databricks PAT", re.compile(r"\bdapi[0-9a-f]{32}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "hardcoded password assignment",
        re.compile(r"(?i)\b(password|passwd|secret)\s*[:=]\s*['\"][^'\"$#{]{8,}['\"]"),
    ),
]


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def iter_files(subdir: str, suffixes: tuple[str, ...]) -> list[Path]:
    base = ARTIFACTS / subdir
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in suffixes)


# ---------------------------------------------------------------- stage 1


def stage_schema(report: Report) -> None:
    """Parse every artifact and scan it for credentials."""
    if not ARTIFACTS.is_dir():
        report.error("artifacts/ directory is missing")
        return

    yaml_files = iter_files("configs", (".yml", ".yaml")) + iter_files("jobs", (".yml", ".yaml"))
    for path in yaml_files:
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            report.error(f"{rel(path)}: invalid YAML - {exc}")
    report.note(f"parsed {len(yaml_files)} YAML artifact(s)")

    py_files = iter_files("dags", (".py",))
    for path in py_files:
        source = path.read_text(encoding="utf-8")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            report.error(f"{rel(path)}: syntax error line {exc.lineno} - {exc.msg}")
    report.note(f"compiled {len(py_files)} DAG file(s)")

    sql_files = iter_files("dbt", (".sql",))
    for path in sql_files:
        if not path.read_text(encoding="utf-8").strip():
            report.error(f"{rel(path)}: empty SQL file")
    report.note(f"checked {len(sql_files)} SQL file(s)")

    scanned = 0
    for path in sorted(p for p in ARTIFACTS.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for label, pattern in CREDENTIAL_PATTERNS:
            match = pattern.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                report.error(f"{rel(path)}:{line}: possible {label} committed to an artifact")
    report.note(f"credential-scanned {scanned} file(s)")


# ---------------------------------------------------------------- stage 2


def declares_dag(source: str) -> bool:
    """True when the module looks like it builds an Airflow DAG."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in {"DAG", "dag", "build_dags", "create_dag"}:
                return True
        if isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                    if name == "DAG":
                        return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if (getattr(target, "id", None) or getattr(target, "attr", None)) == "dag":
                    return True
    return False


def stage_dag_structure(report: Report) -> None:
    dag_files = iter_files("dags", (".py",))
    if not dag_files:
        report.warn("artifacts/dags contains no Python files")
        return

    without_dag = [rel(p) for p in dag_files if not declares_dag(p.read_text(encoding="utf-8"))]
    for name in without_dag:
        report.warn(
            f"{name}: no DAG object detected - will deploy but Airflow will not schedule it"
        )

    report.note(f"{len(dag_files) - len(without_dag)}/{len(dag_files)} DAG file(s) declare a DAG")


# ---------------------------------------------------------------- stage 3


def stage_coverage(report: Report) -> None:
    configs = {p.stem for p in iter_files("configs", (".yml", ".yaml"))}
    dags = {p.stem for p in iter_files("dags", (".py",))}

    for name in sorted(configs - dags):
        report.warn(f"config '{name}' has no matching DAG (expected during development)")
    for name in sorted(dags - configs):
        report.warn(f"DAG '{name}' has no matching config (possible orphan)")

    report.note(
        f"coverage: {len(configs)} config(s), {len(dags)} DAG(s), {len(configs & dags)} paired"
    )


# ---------------------------------------------------------------- output


def emit(report: Report) -> None:
    lines: list[str] = ["## Artifact validation", ""]

    for label, items, icon in (
        ("Errors", report.errors, "❌"),
        ("Warnings", report.warnings, "⚠️"),
    ):
        if items:
            lines.append(f"### {icon} {label} ({len(items)})")
            lines.extend(f"- {item}" for item in items)
            lines.append("")

    if report.notes:
        lines.append("### Checks run")
        lines.extend(f"- {note}" for note in report.notes)
        lines.append("")

    if not report.errors:
        lines.append("**Result:** passed" + (" with warnings" if report.warnings else ""))
    else:
        lines.append(f"**Result:** failed - {len(report.errors)} error(s)")

    summary = "\n".join(lines)

    # Developers run this on Windows, where stdout defaults to cp1252 and the
    # status icons would raise UnicodeEncodeError.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(summary)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n")


def main() -> int:
    report = Report()
    stage_schema(report)
    stage_dag_structure(report)
    stage_coverage(report)
    emit(report)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
