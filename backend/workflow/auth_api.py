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

from .accounts import Account, AccountStore, AuthError, ADMIN_ROLES, ROLE_BANK_ADMIN, ROLE_CUSTOMER


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


def require_bank_admin(request) -> Account:
    """Require the real bank-administrator role for the Bank Admin portal.

    ``DEMO_ADMIN`` remains available for deliberately separate demo tooling,
    but it must not be able to inspect or authorise the full Mock Bank ledger.
    """

    return require_role(
        request,
        {ROLE_BANK_ADMIN},
        "The Mock Bank portal is restricted to bank administrator accounts.",
    )


def require_role(request, roles: set[str], message: str) -> Account:
    """The caller must be signed in with one of ``roles``; 403 with ``message`` otherwise."""

    account = current_account(request)
    if account is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    if account.role not in roles:
        raise HTTPException(status_code=403, detail=message)
    return account


def require_account(request) -> Account:
    """Any signed-in account."""

    return require_customer(request)


def require_signed_in_customer(request) -> Account:
    """Only a CUSTOMER account: customers send messages, the bank side does not."""

    return require_role(
        request,
        {ROLE_CUSTOMER},
        "Only customer accounts can send a message. Bank and demo administrators review cases instead.",
    )


def require_demo_admin(request) -> Account:
    from .accounts import ROLE_DEMO_ADMIN

    return require_role(
        request, {ROLE_DEMO_ADMIN}, "Test scenarios can only be run by a demo administrator."
    )


def assert_can_approve(account: Account, owner_account_id: str | None) -> None:
    """Approval belongs to the bank side, and never to the person who asked.

    Call after ``assert_owner``, so a customer probing someone else's case still
    gets a plain 404 and learns nothing about it.
    """

    if account.role not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail=(
                "Customers can request payments but cannot approve them. "
                "A bank administrator must approve or reject this payment."
            ),
        )
    if owner_account_id and owner_account_id == account.account_id:
        raise HTTPException(
            status_code=403,
            detail="You requested this payment, so you cannot approve or reject it yourself.",
        )


def assert_owner(account: Account, owner_account_id: str | None) -> None:
    """Raise unless the account owns the record.

    Legacy rows (no owner) are refused too: an unverified historical record must
    not be revealed to a customer who happens to ask for its ID.
    """

    if account.role in ADMIN_ROLES:
        return
    if not owner_account_id or owner_account_id != account.account_id:
        raise HTTPException(status_code=404, detail="Not found.")
