"""FastAPI authentication and tenancy helpers.

Every customer-facing endpoint must resolve to an account and scope its query to
that account. Admin-only endpoints check the role.

The session token travels in the ``Authorization: Bearer <token>`` header. It is
also accepted as an ``X-Account-Token`` header and as a query parameter, so the
existing browser frontend can use it without a build step.
"""

from __future__ import annotations

from typing import Any

from fastapi import Cookie, Header, HTTPException, Query

from .accounts import Account, AccountStore, AuthError, ADMIN_ROLES, ROLE_CUSTOMER


class AccountContext:
    """Resolved account, injected into endpoints."""

    def __init__(self, store: AccountStore):
        self.store = store

    def resolve(
        self,
        authorization: str | None,
        x_account_token: str | None,
        token_query: str | None,
    ) -> Account | None:
        raw = None
        if authorization and authorization.lower().startswith("bearer "):
            raw = authorization[7:].strip()
        elif x_account_token:
            raw = x_account_token.strip()
        elif token_query:
            raw = token_query.strip()
        return self.store.account_for_token(raw)


def install_auth(app: Any, database_path) -> AccountStore:
    """Attach the account store and the request helpers to the app."""

    store = AccountStore(database_path)
    app.state.accounts = store
    app.state.account_context = AccountContext(store)
    return store


def require_account(authorization, x_account_token, token_query) -> Account:
    """Dependency: the caller must be signed in."""

    from fastapi import Request

    raise NotImplementedError("use app.state.account_context.resolve")


def current_account(request) -> Account | None:
    """Resolve the caller from the request, or None when anonymous."""

    resolver = request.app.state.account_context
    return resolver.resolve(
        request.headers.get("authorization"),
        request.headers.get("x-account-token"),
        request.query_params.get("token"),
    )


def require_customer(request) -> Account:
    """Any signed-in account may act as a customer."""

    account = current_account(request)
    if account is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return account


def require_admin(request) -> Account:
    """Only BANK_ADMIN or DEMO_ADMIN may see internal bank records."""

    account = current_account(request)
    if account is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    if account.role not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail=(
                "Internal Mock Bank records are restricted to bank administrators. "
                "Customers see their own payment history instead."
            ),
        )
    return account


def assert_owner(account: Account, owner_account_id: str | None) -> None:
    """Raise unless the account owns the record.

    Legacy rows (no owner) are refused too: an unverified historical record must
    not be revealed to a customer who happens to ask for its ID.
    """

    if account.role in ADMIN_ROLES:
        return
    if not owner_account_id or owner_account_id != account.account_id:
        raise HTTPException(status_code=404, detail="Not found.")
