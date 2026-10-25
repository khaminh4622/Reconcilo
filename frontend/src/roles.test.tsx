import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { navFor, Page, SessionContext, type Me } from "./App";
import { Answer, CaseView } from "./pages/CasePage";
import { ApprovalsList } from "./pages/Approvals";
import { currentPayments } from "./pages/CustomerDashboard";
import type { Role, WorkflowCase } from "./types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
afterEach(() => container?.remove());

function render(ui: ReactNode, role: Role) {
  const me: Me = { account: { account_id: "a1", email: "x@example.test", display_name: "X", role, created_at: "" }, payment_history: [], receipts: [] };
  container = document.createElement("div");
  document.body.appendChild(container);
  act(() => createRoot(container).render(<SessionContext.Provider value={{ me, health: null }}>{ui}</SessionContext.Provider>));
  return { me, text: () => container.textContent ?? "", buttons: () => [...container.querySelectorAll("button")].map((b) => b.textContent) };
}

// Shape of GET /workflow/cases/{id} for a case awaiting approval (backend README).
const awaiting: WorkflowCase = {
  case_id: "case-1", created_at: "", updated_at: "", status: "AWAITING_APPROVAL", intent: "new_payment_request",
  customer_name: null, vendor_name: "Northstar Office Supplies LLC", invoice_number: "INV-7781", amount: "1280.00", currency: "USD",
  analysis: null, owner_account_id: "cust-1", approval: null, payment: null, reconciliation: null, receipt: null,
  audit: [], outcome: null, requires_human: true, pending_reason: null, customer_response: null, source_channel: "message",
  agent_runs: [
    { agent: "agent_3", agent_name: "", status: "completed", order: 0, phase: null, summary: null, started_at: null, completed_at: null },
    { agent: "agent_1", agent_name: "", status: "completed", order: 1, phase: null, summary: null, started_at: null, completed_at: null },
    { agent: "human_approval", agent_name: "", status: "requires_human", order: 2, phase: null, summary: null, started_at: null, completed_at: null },
  ],
  reports: [{ agent: "agent_1", agent_name: "", summary: "No comparison records.", findings: [], next_action: null,
    requires_human: true, resolved: false, data: { status: "INSUFFICIENT_INFORMATION" }, completed_at: null }],
  approval_policy: {
    required: true, eligible_roles: ["BANK_ADMIN", "DEMO_ADMIN"],
    requester: { account_id: "cust-1", display_name: "Demo Customer A", email: "customer.a@ghostledger.test" },
    blocked: [{ user: "Demo Customer A", reason: "The account that requested a payment cannot approve it." }],
  },
};

describe("approval by role", () => {
  it("a customer sees no decision buttons, only the bank-review notice", () => {
    const view = render(<CaseView c={awaiting} />, "CUSTOMER");
    expect(view.buttons()).not.toContain("Approve payment");
    expect(view.text()).toContain("Bank reviewing payment");
    expect(view.text()).toContain("You requested this, so you can't approve or decline it");
    expect(view.text()).toContain("The account that requested a payment cannot approve it.");
  });

  it("a bank admin is directed to the dedicated Bank Admin portal", () => {
    const view = render(<CaseView c={awaiting} />, "BANK_ADMIN");
    expect(view.buttons()).not.toEqual(expect.arrayContaining(["Approve payment", "Reject"]));
    expect(view.text()).toContain("Bank decision required");
    expect(view.text()).toContain("Open Bank Admin");
  });

  it("case pages do not expose simulation controls outside Bank Admin", () => {
    expect(render(<CaseView c={awaiting} />, "BANK_ADMIN").text()).not.toContain("Developer: simulated bank result");
    expect(render(<CaseView c={awaiting} />, "DEMO_ADMIN").text()).not.toContain("Developer: simulated bank result");
  });
});

describe("navigation by role", () => {
  it("customers get no ledger or admin links", () => {
    const paths = navFor("CUSTOMER").map(([p]) => p);
    expect(paths).toEqual(["dashboard", "new", "invoices", "payments", "investigate", "profile"]);
  });

  it("only bank admins see bank navigation; demo admins see their isolated scenarios", () => {
    expect(navFor("BANK_ADMIN").map(([p]) => p)).toEqual([
      "bank", "approvals", "case", "overview", "ledger", "failures", "activity", "customers",
    ]);
    expect(navFor("DEMO_ADMIN").map(([p]) => p)).toEqual(["scenarios", "demo-script"]);
  });

  it("a customer opening an admin URL is told it is for bank admins", () => {
    const { me } = render(<div />, "CUSTOMER");
    const view = render(<Page me={me} route={["approvals"]} />, "CUSTOMER");
    expect(view.text()).toContain("This area is for bank admins.");
  });

  it("a demo administrator cannot open the Bank Admin page", () => {
    const { me } = render(<div />, "DEMO_ADMIN");
    const view = render(<Page me={me} route={["bank"]} />, "DEMO_ADMIN");
    expect(view.text()).toContain("This area is for bank admins.");
  });

  it("the approvals list shows requester, terms and Agent 1's result", () => {
    const view = render(<ApprovalsList error={null} items={[{
      case_id: "case-1", created_at: "", requester: awaiting.approval_policy!.requester, invoice_number: "INV-7781",
      vendor: "Northstar Office Supplies LLC", amount: "1280.00", currency: "USD",
      agent1: { status: "INSUFFICIENT_INFORMATION", summary: "No comparison records.", needs_human_judgement: true },
    }]} />, "BANK_ADMIN");
    for (const text of ["INV-7781", "Demo Customer A", "1,280.00 USD", "INSUFFICIENT_INFORMATION", "Needs human judgement"]) {
      expect(view.text()).toContain(text);
    }
  });
});

describe("customer dashboard payment display", () => {
  it("shows one current payment for duplicate historical request rows", () => {
    const rows = currentPayments([
      {
        payment_id: "old", case_id: "case-old", invoice_number: "INV-7781", payee: "Northstar Office Supplies LLC",
        amount: "1280.00", currency: "USD", status: "PENDING_APPROVAL", settlement_reference: null,
        failure_code: null, failure_reason: null, created_at: "2026-01-01T00:00:00Z", processed_at: null, case_status: "AWAITING_APPROVAL",
      },
      {
        payment_id: "current", case_id: "case-current", invoice_number: "INV-7781", payee: "Northstar Office Supplies LLC",
        amount: "1280.00", currency: "USD", status: "SETTLED", settlement_reference: "MB-123",
        failure_code: null, failure_reason: null, created_at: "2026-01-02T00:00:00Z", processed_at: "2026-01-02T00:01:00Z", case_status: "COMPLETED",
      },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].payment_id).toBe("current");
  });
});

describe("answer before Agent 5 replies", () => {
  const triaged: WorkflowCase = {
    ...awaiting,
    why: "No comparison records are available.",
    next_action: "Approve 1280.00 USD to Northstar Office Supplies LLC, or reject it.",
  };

  it("a customer sees why, but not the reviewer's approve step", () => {
    const view = render(<Answer c={triaged} isCustomer />, "CUSTOMER");
    expect(view.text()).toContain("No comparison records are available.");
    expect(view.text()).not.toContain("Approve 1280.00 USD");
  });

  it("an admin also gets next_action as the one next step", () => {
    const view = render(<Answer c={triaged} isCustomer={false} />, "BANK_ADMIN");
    expect(view.text()).toContain("Approve 1280.00 USD to Northstar Office Supplies LLC, or reject it.");
  });

  it("Agent 5's answer wins once it exists", () => {
    const answered: WorkflowCase = { ...triaged, customer_response: {
      case_status: "COMPLETED", outcome: "PAID", headline: "Payment confirmed", answer: "Your payment is confirmed.",
      next_steps: ["No action is needed."], payment_settled: true, missing_information: [], generated_by: "deterministic",
    } };
    const view = render(<Answer c={answered} isCustomer />, "CUSTOMER");
    expect(view.text()).toContain("Payment confirmed");
    expect(view.text()).not.toContain("No comparison records");
  });
});
