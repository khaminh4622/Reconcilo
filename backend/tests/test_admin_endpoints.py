"""Bank-side views: payment failures, activity, customers and an overview."""

from __future__ import annotations

import pytest

from workflow.accounts import ROLE_BANK_ADMIN, ROLE_DEMO_ADMIN

from http_helpers import auth, invoice_text, make_admin, make_app, reply, signup, upload

ADMIN_ROUTES = ("/admin/payment-failures", "/admin/activity", "/admin/customers", "/admin/overview")


@pytest.fixture
def world(workspace_tmp_path):
    """Two customers: one settled payment, one failed payment, one awaiting approval."""

    app, client = make_app(workspace_tmp_path, "admin-world")
    alice, alice_id = signup(client, "alice@example.test", "Alice")
    bob, bob_id = signup(client, "bob@example.test", "Bob")
    admin, admin_id = make_admin(app, client, "bank@example.test")

    def request(token, invoice, amount):
        return upload(app, client, token, invoice_text(invoice, amount), reply(invoice, amount)).json()

    ok = request(alice, "INV-AD-OK", "301.10")
    client.post(f"/workflow/cases/{ok['case_id']}/approve", json={}, headers=auth(admin))
    failed = request(bob, "INV-AD-FAIL", "302.20")
    client.post(
        f"/workflow/cases/{failed['case_id']}/approve", json={"simulate": "FAILED"}, headers=auth(admin)
    )
    waiting = request(bob, "INV-AD-WAIT", "303.30")
    return {
        "app": app, "client": client, "admin": admin, "admin_id": admin_id,
        "alice": alice, "alice_id": alice_id, "bob": bob, "bob_id": bob_id,
        "failed_case": failed["case_id"], "waiting_case": waiting["case_id"],
    }


def test_customers_get_403_and_anonymous_401(world):
    client = world["client"]
    for url in ADMIN_ROUTES:
        assert client.get(url).status_code == 401, url
        assert client.get(url, headers=auth(world["alice"])).status_code == 403, url


def test_demo_admin_has_the_same_views(world):
    demo, _ = make_admin(world["app"], world["client"], "demo@example.test", role=ROLE_DEMO_ADMIN)
    for url in ADMIN_ROUTES:
        assert world["client"].get(url, headers=auth(demo)).status_code == 200, url


def test_payment_failures_link_case_customer_and_reason(world):
    data = world["client"].get("/admin/payment-failures", headers=auth(world["admin"])).json()

    sources = {f["source"] for f in data["failures"]}
    assert sources == {"payment_attempt", "ledger"}
    for failure in data["failures"]:
        assert failure["invoice_number"] == "INV-AD-FAIL"
        assert failure["failure_code"]
        assert failure["failure_reason"]
        assert failure["case_id"] == world["failed_case"]
        assert failure["customer"]["account_id"] == world["bob_id"]
        assert failure["customer"]["display_name"] == "Bob"


def test_activity_is_newest_first_across_cases(world):
    data = world["client"].get("/admin/activity?limit=5", headers=auth(world["admin"])).json()

    entries = data["activity"]
    assert data["count"] == len(entries) == 5
    assert [e["at"] for e in entries] == sorted((e["at"] for e in entries), reverse=True)
    assert {"case_id", "invoice_number", "customer", "at", "actor", "action", "detail"} <= set(entries[0])
    everything = world["client"].get("/admin/activity?limit=500", headers=auth(world["admin"])).json()
    approvals = [e for e in everything["activity"] if e["action"] == "approved"]
    assert approvals and all(e["actor_account_id"] == world["admin_id"] for e in approvals)
    assert len({e["case_id"] for e in everything["activity"]}) == 3


def test_customers_list_counts_and_never_exposes_secrets(world):
    response = world["client"].get("/admin/customers", headers=auth(world["admin"]))
    data = response.json()

    by_email = {c["email"]: c for c in data["customers"]}
    assert set(by_email) == {"alice@example.test", "bob@example.test"}, "only CUSTOMER accounts"
    assert by_email["alice@example.test"]["case_count"] == 1
    assert by_email["bob@example.test"]["case_count"] == 2
    assert by_email["bob@example.test"]["last_activity"]
    body = response.text.lower()
    for secret in ("password", "salt", "hash", "token"):
        assert secret not in body, secret
    assert world["alice"] not in response.text


def test_overview_counts(world):
    data = world["client"].get("/admin/overview", headers=auth(world["admin"])).json()

    assert data["cases_total"] == 3
    assert data["cases_by_status"] == {"AWAITING_APPROVAL": 1, "COMPLETED": 1, "REQUIRES_REVIEW": 1}
    assert data["cases_needing_a_person"] == 2
    assert data["payments"] == {"settled": 1, "failed": 1, "pending": 0}
    runs = data["agent_runs"]
    assert runs["agent_3"] == 3 and runs["agent_1"] == 3
    assert runs["agent_2"] == 1 and runs["agent_4"] == 1 and runs["agent_5"] == 2
    assert runs["payment_simulator"] == 2


def test_bank_admin_role_constant_is_used(world):
    account = world["app"].state.accounts.get(world["admin_id"])
    assert account.role == ROLE_BANK_ADMIN


def test_activity_limit_reads_newest_entries_without_loading_everything(world):
    """Only `limit` cases are loaded, yet the result equals the full ordering."""

    client, admin = world["client"], auth(world["admin"])
    everything = client.get("/admin/activity?limit=500", headers=admin).json()["activity"]
    for limit in (1, 3, 7):
        top = client.get(f"/admin/activity?limit={limit}", headers=admin).json()["activity"]
        assert [e["at"] for e in top] == [e["at"] for e in everything[:limit]]


def test_full_case_listing_is_not_capped(world):
    repository = world["app"].state.workflow_repository
    assert len(repository.list_full_cases()) == 3
    assert len(repository.list_full_cases(1)) == 1
