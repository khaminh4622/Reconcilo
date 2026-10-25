"""Shared pytest fixtures.

Agent 3 tests need throw-away directories. pytest's built-in ``tmp_path`` keeps
its directories under the system temp root and inspects that root by name on
every run, which some Windows sandbox configurations do not allow. The project
therefore resolves its test scratch space inside the workspace and creates a
unique directory per test, so nothing has to be deleted or enumerated.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest


SCRATCH_ROOT = Path(__file__).resolve().parents[1] / ".test-scratch"


@pytest.fixture
def workspace_tmp_path() -> Path:
    """Return a fresh, isolated directory inside the project workspace.

    Unlike the built-in ``tmp_path`` this never inspects the system temp root,
    so it works under a restricted Windows sandbox. Unit tests that only need a
    private directory should use the built-in ``tmp_path``; this fixture exists
    for the suite itself to run in confined environments.
    """

    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    directory = SCRATCH_ROOT / uuid4().hex[:12]
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def workspace_tmpdir(workspace_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``tempfile`` into the isolated workspace directory as well."""

    monkeypatch.setattr(tempfile, "tempdir", str(workspace_tmp_path))
    monkeypatch.setenv("TMPDIR", str(workspace_tmp_path))
    monkeypatch.setenv("TEMP", str(workspace_tmp_path))
    monkeypatch.setenv("TMP", str(workspace_tmp_path))
    return workspace_tmp_path


@pytest.fixture(autouse=True)
def _isolate_repository_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests away from the developer's real database.

    Without this, any test that builds the app would write workflow cases into
    backend/data/agent3.db and pollute the dashboard with fixture rows.
    """

    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    isolated = SCRATCH_ROOT / f"db-{uuid4().hex[:10]}.db"
    monkeypatch.setenv("AGENT3_DB_PATH", str(isolated))
    monkeypatch.delenv("RUN_LIVE_LLM_TEST", raising=False)
    # A developer's .env may enable demo accounts; tests start with none.
    monkeypatch.delenv("GHOSTLEDGER_DEMO_ACCOUNTS", raising=False)
    # Each test starts with a fresh synthetic provider so one test's payment
    # outcomes never leak into another.
    from workflow.payment_provider import reset_provider

    reset_provider()
