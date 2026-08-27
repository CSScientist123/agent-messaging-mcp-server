"""Runs the schema constraint suite inside pytest, and pins the schema to the vault.

`test_schema_constraints.py` is the vault's own runner, kept byte-identical so it can be
copied back and forth without drift. It guards itself with `if __name__ == "__main__"`, so
pytest never collects it. This wrapper is what puts it in the suite.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = REPO / "schema" / "schema.sql"
RUNNER = REPO / "tests" / "test_schema_constraints.py"
VAULT_SCHEMA = pathlib.Path(
    "/mnt/c/Data/Books/Brains/polling-mechanism/Concepts/Schemas/Messaging/schema.sql"
)


def test_schema_constraints_all_pass():
    """Every constraint in the schema actually bites -- not merely that the DDL parses."""
    proc = subprocess.run(
        [sys.executable, str(RUNNER), str(SCHEMA)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        "schema constraint suite failed:\n"
        + "\n".join(l for l in proc.stdout.splitlines() if l.startswith("FAIL"))
    )
    assert "0 failed" in proc.stdout, proc.stdout[-500:]


def test_shipped_schema_matches_the_vault():
    """The documentation is the source of truth; this repo ships a copy, not a fork.

    Skipped rather than failed when the vault is not mounted -- a missing mount is not
    evidence of drift, and a test that cannot observe its subject must not claim a verdict.
    """
    if not VAULT_SCHEMA.exists():
        import pytest
        pytest.skip(f"vault not mounted at {VAULT_SCHEMA}")
    assert SCHEMA.read_bytes() == VAULT_SCHEMA.read_bytes(), (
        "schema/schema.sql has diverged from the vault's canonical copy"
    )


def test_documentation_matches_the_codebase():
    """Run the doc-vs-code audit as part of the suite.

    Documentation drifts silently: a rename, a changed seed value, a new
    capability, and the prose keeps reading exactly like the truth. Running the
    audit here means drift fails a test rather than waiting to be noticed.
    """
    proc = subprocess.run(
        [sys.executable, str(REPO / "tests" / "doc_consistency.py")],
        cwd=str(REPO), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        "documentation has drifted from the codebase:\n"
        + "\n".join(l for l in proc.stdout.splitlines() if l.strip().startswith("["))
        + ("\n" + proc.stderr[-500:] if proc.stderr.strip() else "")
    )
