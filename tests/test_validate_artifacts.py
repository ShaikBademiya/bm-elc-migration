"""Tests for the artifact validation gate.

The coverage report is the part most likely to fail silently: if job identity is
computed wrongly it reports "all paired" while comparing nonsense, and a broken
gate is worse than no gate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "validate_artifacts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("validate_artifacts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validate_artifacts = load_module()


def test_job_key_flat_layout_uses_the_stem() -> None:
    base = validate_artifacts.ARTIFACTS / "configs"
    assert validate_artifacts.job_key(base / "orders.yaml", "configs") == "orders"
    assert validate_artifacts.job_key(base / "returns.yml", "configs") == "returns"


def test_job_key_nested_layout_uses_the_job_directory() -> None:
    """TRD-002 :461 uses {job}/config.yaml, where every stem is 'config'."""
    base = validate_artifacts.ARTIFACTS / "configs"
    assert validate_artifacts.job_key(base / "orders" / "config.yaml", "configs") == "orders"
    assert validate_artifacts.job_key(base / "returns" / "config.yaml", "configs") == "returns"


def test_nested_jobs_do_not_collapse_to_one_key() -> None:
    """The regression this guards: N jobs must not reduce to a single entry."""
    base = validate_artifacts.ARTIFACTS / "configs"
    paths = [base / job / "config.yaml" for job in ("orders", "returns", "shipments")]
    keys = {validate_artifacts.job_key(p, "configs") for p in paths}
    assert keys == {"orders", "returns", "shipments"}


def test_declares_dag_detects_real_dags_and_rejects_stubs() -> None:
    assert not validate_artifacts.declares_dag("print('hello')")
    assert validate_artifacts.declares_dag("with DAG('x') as dag:\n    pass")
    assert validate_artifacts.declares_dag("dag = DAG('x')")
    assert validate_artifacts.declares_dag("@dag\ndef my_pipeline():\n    pass")
    assert validate_artifacts.declares_dag("build_dags(manifest)")


def test_credential_patterns_catch_common_secrets() -> None:
    hits = []
    samples = [
        "-----BEGIN RSA PRIVATE KEY-----",
        '{"type": "service_account"}',
        "AKIAIOSFODNN7EXAMPLE",
        "password: 'hunter2issolong'",
    ]
    for sample in samples:
        hits.append(any(p.search(sample) for _, p in validate_artifacts.CREDENTIAL_PATTERNS))
    assert all(hits), f"missed one of the samples: {hits}"


def test_credential_patterns_do_not_fire_on_templated_values() -> None:
    """Secrets referenced through a template are how configs are supposed to look."""
    benign = [
        "password: ${DB_PASSWORD}",
        "secret: $SECRET_NAME",
        'password: "{{ var.value.db_pw }}"',
    ]
    for sample in benign:
        assert not any(p.search(sample) for _, p in validate_artifacts.CREDENTIAL_PATTERNS), sample
