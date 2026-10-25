import { describe, expect, it } from "vitest";
import { detailText } from "./api";
import { riskBadge, statusLabel, statusTone, stepLabel, visiblePath } from "./labels";
import { mergeFailures, type FailureRow } from "./pages/Failures";
import type { AgentRun, CaseStatus } from "./types";

// [agent, status, phase?] in the order the backend returns them (from the
// backend's conformance tests, tests/test_diagram_conformance.py).
const runs = (spec: [string, AgentRun["status"], string?][]): AgentRun[] =>
  spec.map(([agent, status, phase], order) => ({
    agent, status, order, phase: phase ?? null, agent_name: agent, summary: null, started_at: null, completed_at: null,
  }));

const labels = (r: AgentRun[]) => visiblePath(r).map(stepLabel);

describe("status labels", () => {
  it("maps every backend status to a plain label", () => {
    const all: CaseStatus[] = ["RECEIVED", "ANALYZING", "VALIDATING", "ROUTED", "INVESTIGATING", "AWAITING_APPROVAL",
      "APPROVED", "EXECUTING", "PENDING", "COMPLETED", "FAILED", "REQUIRES_REVIEW"];
    for (const s of all) expect(statusLabel(s)).not.toBe(s);
    expect(statusLabel("REQUIRES_REVIEW")).toBe("Needs a person");
    expect(statusLabel("PENDING")).toBe("Payment not settled yet");
  });

  it("reserves green for confirmed, red for failure, amber for attention", () => {
    expect(statusTone("COMPLETED")).toBe("confirmed");
    expect(statusTone("FAILED")).toBe("failure");
    expect(statusTone("REQUIRES_REVIEW")).toBe("attention");
    expect(statusTone("AWAITING_APPROVAL")).toBe("attention");
  });

  it("never shows payment confirmed unless the backend says it settled", () => {
    expect(riskBadge({ status: "COMPLETED", customer_response: null, requires_human: false })[0]).not.toMatch(/confirmed/i);
    const settled = { status: "COMPLETED" as const, requires_human: false,
      customer_response: { payment_settled: true } as never };
    expect(riskBadge(settled)).toEqual(["Payment confirmed", "confirmed"]);
  });
});

describe("path from agent_runs", () => {
  it("Path A success", () => {
    expect(labels(runs([["agent_3", "completed"], ["agent_1", "completed"], ["human_approval", "completed"],
      ["payment_simulator", "completed"], ["agent_2", "completed"], ["agent_5", "completed", "confirmation"]]))).toEqual([
      "Agent 3 · Reads your request", "Agent 1 · Duplicate check", "Bank approval",
      "Simulated payment", "Agent 2 · Bank reconciliation", "Agent 5 · Confirms result",
    ]);
  });

  it("Path A failure hides the skipped Agent 2 and shows Agent 4", () => {
    expect(labels(runs([["agent_3", "completed"], ["agent_1", "completed"], ["human_approval", "completed"],
      ["payment_simulator", "completed"], ["agent_2", "skipped"], ["agent_4", "requires_human"],
      ["agent_5", "completed", "failure_report"], ["human_review", "requires_human"]]))).toEqual([
      "Agent 3 · Reads your request", "Agent 1 · Duplicate check", "Bank approval", "Simulated payment",
      "Agent 4 · Payment exception", "Agent 5 · Explains the failure", "Human review",
    ]);
  });

  it("Path B status dispute", () => {
    expect(labels(runs([["agent_3", "completed"], ["agent_2", "requires_human"],
      ["agent_5", "requires_human", "explanation"], ["human_review", "requires_human"]]))).toEqual([
      "Agent 3 · Reads your request", "Agent 2 · Bank reconciliation", "Agent 5 · Explains outcome", "Human review",
    ]);
  });

  it("human review, in the backend's order", () => {
    expect(labels(runs([["agent_3", "completed"], ["human_review", "requires_human"], ["agent_5", "completed", "explanation"]])))
      .toEqual(["Agent 3 · Reads your request", "Human review", "Agent 5 · Explains outcome"]);
  });

  it("sorts by order rather than array position", () => {
    const r = runs([["agent_3", "completed"], ["agent_5", "waiting"]]).reverse();
    expect(visiblePath(r).map((x) => x.agent)).toEqual(["agent_3", "agent_5"]);
  });
});

describe("payment failures", () => {
  it("merges the attempt and ledger rows of one failure", () => {
    const row = (source: FailureRow["source"]): FailureRow => ({
      source, failure_code: "PAYMENT_REJECTED", failure_reason: "Rejected.", invoice_number: "INV-1",
      amount: "10.00", currency: "USD", at: null, case_id: "c1", customer: null,
    });
    const merged = mergeFailures([row("ledger"), row("payment_attempt")]);
    expect(merged).toHaveLength(1);
    expect(merged[0].sources).toEqual(["ledger", "payment_attempt"]);
  });
});

describe("backend errors", () => {
  it("uses the backend's detail text", () => {
    expect(detailText(403, { detail: "Customers can request payments but cannot approve them." }))
      .toBe("Customers can request payments but cannot approve them.");
    expect(detailText(413, null)).toBe("That is too large to send.");
  });
});
