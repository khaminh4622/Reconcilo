"""Customers request payments; only the bank side approves them.

POST /workflow/cases/{id}/approve and /reject need BANK_ADMIN or DEMO_ADMIN,
never the account that requested the payment, and the approver is always the
signed-in account rather than a name typed into the request body.
"""

from __future__ import annotations

from workflow.accounts import ROLE_BANK_ADMIN, ROLE_DEMO_ADMIN

from http_helpers import auth, invoice_text, make_admin, make_app, reply, signup, upload


def request_payment(app, client, token, invoice="INV-BS-1", amount="263.17"):
    response = upload(app, client, token, invoice_text(invoice, amount), reply(invoice, amount))
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "AWAITING_APPROVAL"
    return response.json()


def test_owner_customer_gets_plain_403_on_approve_and_reject(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "owner403")
    customer, _ = signup(client, "c@example.test")
    case = request_payment(app, client, customer)

    for action in ("approve", "reject"):
        response = client.post(f"/workflow/cases/{case['case_id']}/{action}", json={}, headers=auth(customer))
        assert response.status_code == 403
        assert "Customers can request payments but cannot approve them" in response.json()["detail"]
    assert client.get(f"/workflow/cases/{case['case_id']}", headers=auth(customer)).json()["status"] == (
        "AWAITING_APPROVAL"
    )


def test_other_customer_still_gets_404_and_anonymous_401(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "probe")
    owner, _ = signup(client, "owner@example.test")
    other, _ = signup(client, "other@example.test")
    case = request_payment(app, client, owner)

    assert client.post(f"/workflow/cases/{case['case_id']}/approve", json={}, headers=auth(other)).status_code == 404
    assert client.post(f"/workflow/cases/{case['case_id']}/approve", json={}).status_code == 401


def test_admin_approves_and_approver_comes_from_the_account(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "admin-ok")
    customer, _ = signup(client, "c@example.test")
    admin, admin_id = make_admin(app, client, "bank@example.test", name="Riley Bank")
    case = request_payment(app, client, customer)

    response = client.post(
        f"/workflow/cases/{case['case_id']}/approve",
        json={"approved_by": "Somebody Else", "note": "ok"},
        headers=auth(admin),
    )

    assert response.status_code == 200, response.text
    paid = response.json()
    assert paid["status"] == "COMPLETED"
    assert paid["approval"]["approved_by"] == f"Riley Bank ({admin_id})"
    assert paid["approval"]["approved_by_account_id"] == admin_id
    assert "Somebody Else" not in str(paid["audit"])
    approved = next(e for e in paid["audit"] if e["action"] == "approved")
    assert approved["actor_account_id"] == admin_id
    # Still bound to the exact payment terms.
    assert paid["approval"]["invoice_number"] == "INV-BS-1"
    assert paid["approval"]["amount"] == "263.17"


def test_demo_admin_may_reject(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "demo-reject")
    customer, _ = signup(client, "c@example.test")
    demo, demo_id = make_admin(app, client, "demo@example.test", role=ROLE_DEMO_ADMIN, name="Demo")
    case = request_payment(app, client, customer)

    response = client.post(
        f"/workflow/cases/{case['case_id']}/reject",
        json={"rejected_by": "ignored", "reason": "Duplicate vendor record."},
        headers=auth(demo),
    )

    assert response.status_code == 200, response.text
    rejected = response.json()
    assert rejected["status"] == "FAILED"
    entry = next(e for e in rejected["audit"] if e["action"] == "rejected")
    assert entry["actor_account_id"] == demo_id
    assert rejected["customer_response"]["outcome"] == "REJECTED"


def test_admin_cannot_approve_their_own_request(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "self")
    admin, _ = make_admin(app, client, "bank@example.test")
    # Any signed-in account may upload, including an admin.
    case = request_payment(app, client, admin)

    response = client.post(f"/workflow/cases/{case['case_id']}/approve", json={}, headers=auth(admin))
    assert response.status_code == 403
    assert "cannot approve or reject it yourself" in response.json()["detail"]


def test_approvals_queue_lists_requester_terms_and_agent1(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "queue")
    customer, customer_id = signup(client, "c@example.test", "Customer Q")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = request_payment(app, client, customer, "INV-BS-Q", "275.00")

    assert client.get("/workflow/approvals", headers=auth(customer)).status_code == 403
    assert client.get("/workflow/approvals").status_code == 401
    queue = client.get("/workflow/approvals", headers=auth(admin)).json()

    item = next(i for i in queue["approvals"] if i["case_id"] == case["case_id"])
    assert item["requester"] == {"account_id": customer_id, "display_name": "Customer Q", "email": "c@example.test"}
    assert item["invoice_number"] == "INV-BS-Q"
    assert item["vendor"] and item["amount"] == "275.00" and item["currency"] == "USD"
    assert item["agent1"]["status"] and item["agent1"]["summary"]
    assert item["agent1"]["completed_at"]


def test_case_exposes_approval_policy(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "policy")
    customer, customer_id = signup(client, "c@example.test", "Customer P")
    case = request_payment(app, client, customer)

    view = client.get(f"/workflow/cases/{case['case_id']}", headers=auth(customer)).json()

    policy = view["approval_policy"]
    assert policy["required"] is True
    assert policy["eligible_roles"] == sorted([ROLE_BANK_ADMIN, ROLE_DEMO_ADMIN])
    assert policy["requester"]["account_id"] == customer_id
    users = {entry["user"] for entry in policy["blocked"]}
    assert "Customer P" in users and "CUSTOMER" in users
    assert all(entry["reason"] for entry in policy["blocked"])


def test_case_view_carries_the_same_triage_fields_as_the_list(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "triage")
    customer, _ = signup(client, "c@example.test")
    case = request_payment(app, client, customer, "INV-BS-T", "281.40")

    view = client.get(f"/workflow/cases/{case['case_id']}", headers=auth(customer)).json()
    listed = next(c for c in client.get("/workflow/cases", headers=auth(customer)).json() if c["case_id"] == case["case_id"])

    assert view["next_action"] == listed["next_action"]
    assert view["next_action"].startswith("Approve 281.40 USD")
    assert view["why"] == listed["why"] and view["why"]
    assert view["problems"] == listed["problems"]


def test_my_receipts_link_to_their_case(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "receipt-link")
    customer, _ = signup(client, "c@example.test")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = request_payment(app, client, customer, "INV-BS-R", "292.50")
    client.post(f"/workflow/cases/{case['case_id']}/approve", json={}, headers=auth(admin))

    receipts = client.get("/auth/me", headers=auth(customer)).json()["receipts"]

    assert len(receipts) == 1
    assert receipts[0]["case_id"] == case["case_id"]
    other, _ = signup(client, "other@example.test")
    assert client.get("/auth/me", headers=auth(other)).json()["receipts"] == []
