"""GET /demo/scenarios and POST /demo/scenarios/{name}/run.

``workflow.demo_scenarios.EXPECTED`` was derived by running each fixture once on
a fresh database; this test pins it, so a change in any agent that alters a
demo outcome shows up here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workflow.accounts import ROLE_BANK_ADMIN, ROLE_DEMO_ADMIN
from workflow.demo_scenarios import EXPECTED, FIXTURE_DIR, SCENARIOS

from http_helpers import auth, make_admin, make_app, signup

RAN = {"completed", "blocked", "requires_human"}


def demo_world(workspace_tmp_path, name):
    app, client = make_app(workspace_tmp_path, name)
    customer, customer_id = signup(client, f"{name}-c@example.test", "Demo Customer")
    demo, demo_id = make_admin(app, client, f"{name}-d@example.test", role=ROLE_DEMO_ADMIN, name="Demo Admin")
    return app, client, customer, customer_id, demo, demo_id


def test_every_fixture_pdf_is_catalogued_with_an_expectation():
    on_disk = {path.name for path in Path(FIXTURE_DIR).glob("*.pdf") if not path.name.startswith("_")}
    assert on_disk == set(SCENARIOS)
    assert set(EXPECTED) == set(SCENARIOS)


def test_listing_is_demo_admin_only(workspace_tmp_path):
    app, client, customer, _cid, demo, _did = demo_world(workspace_tmp_path, "list")
    bank, _ = make_admin(app, client, "list-bank@example.test", role=ROLE_BANK_ADMIN)

    assert client.get("/demo/scenarios").status_code == 401
    assert client.get("/demo/scenarios", headers=auth(customer)).status_code == 403
    assert client.get("/demo/scenarios", headers=auth(bank)).status_code == 403
    assert client.post("/demo/scenarios/normal_invoice.pdf/run", json={}, headers=auth(bank)).status_code == 403

    listing = client.get("/demo/scenarios", headers=auth(demo)).json()["scenarios"]
    assert {item["name"] for item in listing} == set(SCENARIOS)
    for item in listing:
        assert item["description"]
        assert item["available"] is True
        assert item["expected"] == EXPECTED[item["name"]]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_outcome_matches_the_pinned_expectation(workspace_tmp_path, name):
    app, client, _customer, customer_id, demo, demo_id = demo_world(workspace_tmp_path, "run")
    expected = EXPECTED[name]

    response = client.post(f"/demo/scenarios/{name}/run", json={}, headers=auth(demo))

    if expected["route"] == "rejected_at_intake":
        assert f"HTTP {response.status_code}" == expected["status"]
        return
    assert response.status_code == 200, response.text
    case = response.json()
    agents = [r["agent"] for r in sorted(case["agent_runs"], key=lambda r: r["order"]) if r["status"] in RAN]
    assert case["intent"] == expected["route"]
    assert case["status"] == expected["status"]
    assert agents == expected["agents"]
    assert (case["customer_response"] or {}).get("outcome") == expected["outcome"]

    # Owned by the demo customer, labelled as a scripted reading, and audited.
    assert case["owner_account_id"] == customer_id
    assert case["source_channel"] == "demo"
    assert case["analysis"]["analysis_source"] == "synthetic_fixture"
    assert case["analysis"]["llm_call"]["invoked"] is False
    demo_entry = next(e for e in case["audit"] if e["action"] == "demo_run")
    assert demo_entry["actor_account_id"] == demo_id


def test_run_targets_a_chosen_customer_and_rejects_unknown_names(workspace_tmp_path):
    app, client, _customer, _cid, demo, _did = demo_world(workspace_tmp_path, "target")
    _other, other_id = signup(client, "target-b@example.test", "Second Customer")

    case = client.post(
        "/demo/scenarios/agent4_new_invoice.pdf/run",
        json={"customer_email": "target-b@example.test"},
        headers=auth(demo),
    ).json()
    assert case["owner_account_id"] == other_id

    assert client.post("/demo/scenarios/../main.py/run", json={}, headers=auth(demo)).status_code == 404
    assert client.post("/demo/scenarios/nope.pdf/run", json={}, headers=auth(demo)).status_code == 404
    assert client.post(
        "/demo/scenarios/agent4_new_invoice.pdf/run",
        json={"customer_account_id": _did}, headers=auth(demo),
    ).status_code == 400, "demo cases belong to CUSTOMER accounts only"


def test_live_mode_needs_a_configured_model(workspace_tmp_path):
    from agent3.extractor import UnavailableExtractor

    app, client, _customer, _cid, demo, _did = demo_world(workspace_tmp_path, "live")
    app.state.service.extractor = UnavailableExtractor("not configured")

    response = client.post(
        "/demo/scenarios/normal_invoice.pdf/run", json={"mode": "live"}, headers=auth(demo)
    )
    assert response.status_code == 409
