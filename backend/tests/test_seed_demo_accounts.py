"""python -m tools.seed_demo_accounts"""

from __future__ import annotations

import pytest

from tools.seed_demo_accounts import LOCAL_DEFAULT_PASSWORD, SeedRefused, main, seed
from workflow.accounts import ROLE_BANK_ADMIN, ROLE_CUSTOMER, ROLE_DEMO_ADMIN, AccountStore


def test_creates_one_of_each_admin_and_two_customers(workspace_tmp_path, monkeypatch):
    monkeypatch.delenv("GHOSTLEDGER_ENV", raising=False)
    database = workspace_tmp_path / "seed.db"

    results = seed(database, "a-strong-password")

    roles = sorted(account.role for account, _created in results)
    assert roles == sorted([ROLE_DEMO_ADMIN, ROLE_BANK_ADMIN, ROLE_CUSTOMER, ROLE_CUSTOMER])
    assert all(created for _account, created in results)
    store = AccountStore(database)
    account, _token = store.sign_in(email="bank.admin@ghostledger.test", password="a-strong-password")
    assert account.role == ROLE_BANK_ADMIN


def test_is_idempotent_and_never_resets_passwords(workspace_tmp_path, monkeypatch):
    monkeypatch.delenv("GHOSTLEDGER_ENV", raising=False)
    database = workspace_tmp_path / "seed.db"
    seed(database, "first-password")

    again = seed(database, "second-password")

    assert not any(created for _account, created in again)
    store = AccountStore(database)
    assert store.count() == 4
    store.sign_in(email="customer.a@ghostledger.test", password="first-password")


def test_refuses_in_production(workspace_tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GHOSTLEDGER_ENV", "production")
    database = workspace_tmp_path / "prod.db"

    with pytest.raises(SeedRefused):
        seed(database, "a-strong-password")
    assert main(["--db", str(database)]) == 2
    assert "production" in capsys.readouterr().err
    assert AccountStore(database).count() == 0


def test_cli_uses_env_password_or_prints_the_local_default(workspace_tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GHOSTLEDGER_ENV", raising=False)
    monkeypatch.delenv("DEMO_ACCOUNT_PASSWORD", raising=False)
    monkeypatch.setattr("tools.seed_demo_accounts.load_dotenv", lambda *a, **k: None)

    assert main(["--db", str(workspace_tmp_path / "default.db")]) == 0
    out = capsys.readouterr().out
    assert LOCAL_DEFAULT_PASSWORD in out
    AccountStore(workspace_tmp_path / "default.db").sign_in(
        email="demo.admin@ghostledger.test", password=LOCAL_DEFAULT_PASSWORD
    )

    monkeypatch.setenv("DEMO_ACCOUNT_PASSWORD", "from-the-environment")
    assert main(["--db", str(workspace_tmp_path / "env.db")]) == 0
    assert "from-the-environment" not in capsys.readouterr().out
    AccountStore(workspace_tmp_path / "env.db").sign_in(
        email="demo.admin@ghostledger.test", password="from-the-environment"
    )


def test_backend_creates_demo_logins_on_startup_when_enabled(workspace_tmp_path, monkeypatch):
    """A fresh clone with GHOSTLEDGER_DEMO_ACCOUNTS=1 can sign in straight away."""

    from http_helpers import make_app

    monkeypatch.delenv("GHOSTLEDGER_ENV", raising=False)
    monkeypatch.delenv("DEMO_ACCOUNT_PASSWORD", raising=False)
    monkeypatch.setenv("GHOSTLEDGER_DEMO_ACCOUNTS", "1")
    app, client = make_app(workspace_tmp_path, "startup-on")

    for email, role in (("demo.admin@ghostledger.test", ROLE_DEMO_ADMIN), ("bank.admin@ghostledger.test", ROLE_BANK_ADMIN),
                        ("customer.a@ghostledger.test", ROLE_CUSTOMER), ("customer.b@ghostledger.test", ROLE_CUSTOMER)):
        response = client.post("/auth/signin", json={"email": email, "password": LOCAL_DEFAULT_PASSWORD})
        assert response.status_code == 200, email
        assert response.json()["account"]["role"] == role

    # Restarting changes nothing and never resets a password.
    make_app(workspace_tmp_path, "startup-on")
    assert app.state.accounts.count() == 4


def test_backend_creates_no_demo_logins_by_default_or_in_production(workspace_tmp_path, monkeypatch):
    from http_helpers import make_app

    app, _client = make_app(workspace_tmp_path, "startup-off")
    assert app.state.accounts.count() == 0

    monkeypatch.setenv("GHOSTLEDGER_DEMO_ACCOUNTS", "1")
    monkeypatch.setenv("GHOSTLEDGER_ENV", "production")
    app, _client = make_app(workspace_tmp_path, "startup-prod")
    assert app.state.accounts.count() == 0
