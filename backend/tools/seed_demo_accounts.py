"""Create the demo accounts: one DEMO_ADMIN, one BANK_ADMIN and two CUSTOMERs.

Public signup only ever creates customers, so this is how a local or demo
deployment gets its bank-side accounts. Running it again changes nothing:
existing accounts are reported and left exactly as they are (their passwords
are not reset).

Usage (from ``ghostledger/backend``):

    python -m tools.seed_demo_accounts
    python -m tools.seed_demo_accounts --db path/to/agent3.db

The password comes from ``DEMO_ACCOUNT_PASSWORD``; without it a local default is
used and printed. The tool refuses to run when ``GHOSTLEDGER_ENV=production``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from workflow.accounts import (  # noqa: E402 - path set up above
    ROLE_BANK_ADMIN,
    ROLE_CUSTOMER,
    ROLE_DEMO_ADMIN,
    Account,
    AccountStore,
)

LOCAL_DEFAULT_PASSWORD = "ghostledger-demo"

DEMO_ACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("demo.admin@ghostledger.test", "Demo Administrator", ROLE_DEMO_ADMIN),
    ("bank.admin@ghostledger.test", "Bank Administrator", ROLE_BANK_ADMIN),
    ("customer.a@ghostledger.test", "Demo Customer A", ROLE_CUSTOMER),
    ("customer.b@ghostledger.test", "Demo Customer B", ROLE_CUSTOMER),
)


class SeedRefused(RuntimeError):
    pass


def _database_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(os.getenv("AGENT3_DB_PATH", str(BACKEND / "data" / "agent3.db")))


def seed(database_path: str | Path, password: str) -> list[tuple[Account, bool]]:
    """Create any missing demo account. Returns (account, created) per account."""

    if (os.getenv("GHOSTLEDGER_ENV") or "").strip().lower() == "production":
        raise SeedRefused("Refusing to create demo accounts because GHOSTLEDGER_ENV=production.")
    if len(password) < 8:
        raise SeedRefused("DEMO_ACCOUNT_PASSWORD must be at least 8 characters.")

    store = AccountStore(database_path)
    existing = {account.email: account for account in store.list_accounts()}
    results: list[tuple[Account, bool]] = []
    for email, display_name, role in DEMO_ACCOUNTS:
        if email in existing:
            results.append((existing[email], False))
            continue
        account = store.sign_up(email=email, password=password, display_name=display_name, role=role)
        results.append((account, True))
    return results


def main(argv: list[str] | None = None) -> int:
    load_dotenv(BACKEND / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="SQLite database (default: AGENT3_DB_PATH or data/agent3.db)")
    args = parser.parse_args(argv)

    password = os.getenv("DEMO_ACCOUNT_PASSWORD") or ""
    using_default = not password
    password = password or LOCAL_DEFAULT_PASSWORD
    database = _database_path(args.db)
    try:
        results = seed(database, password)
    except SeedRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"Demo accounts in {database}:")
    for account, created in results:
        print(f"  {'created ' if created else 'existing'}  {account.role:<10}  {account.email}")
    if any(created for _account, created in results):
        if using_default:
            print(
                f"New accounts use the local default password {LOCAL_DEFAULT_PASSWORD!r}. "
                "Set DEMO_ACCOUNT_PASSWORD to choose another."
            )
        else:
            print("New accounts use the password from DEMO_ACCOUNT_PASSWORD.")
    if not all(created for _account, created in results):
        print("Existing accounts were left unchanged; their passwords were not reset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
