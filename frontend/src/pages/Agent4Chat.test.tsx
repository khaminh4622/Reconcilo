import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionContext, type Me } from "../App";
import type { Agent4Conversation, WorkflowCase } from "../types";
import Agent4Chat from "./Agent4Chat";

vi.mock("../api", () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { api } from "../api";
import { beginChatSession, forgetChatCase } from "../chatSession";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const fetchConversation = vi.mocked(api);

function user(accountId: string): Me {
  return {
    account: { account_id: accountId, email: `${accountId}@example.test`, display_name: accountId, role: "CUSTOMER", created_at: "" },
    payment_history: [],
    receipts: [],
  };
}

function caseOf(caseId: string): WorkflowCase {
  return {
    case_id: caseId, created_at: "", updated_at: "", status: "AWAITING_APPROVAL", intent: "new_payment_request",
    customer_name: null, vendor_name: "Northstar", invoice_number: "INV-1", amount: "10.00", currency: "USD",
    analysis: null, owner_account_id: "user-a", approval: null, payment: null, reconciliation: null, receipt: null,
    audit: [], outcome: null, requires_human: false, pending_reason: null, customer_response: null, source_channel: "upload",
    agent_runs: [], reports: [],
  };
}

function conversation(caseId: string, body: string | null): Agent4Conversation {
  return {
    case_id: caseId,
    invoice_number: "INV-1",
    case_status: "AWAITING_APPROVAL",
    requires_human: false,
    messages: body
      ? [{ message_id: `m-${caseId}`, case_id: caseId, account_id: "user-a", actor: "customer", body, action_id: null, created_at: "2026-01-01T00:00:00Z" }]
      : [],
    payment_status: {
      case_status: "AWAITING_APPROVAL",
      invoice_number: "INV-1",
      amount: "10.00",
      currency: "USD",
      bank_status_label: "No payment record found",
      settlement_verified: false,
      requires_human: false,
      next_steps: [],
      bank: null,
      can_request_payment: false,
    },
    suggested_actions: [],
    next_steps: [],
    escalation: null,
    viewer: { account_id: "user-a", role: "CUSTOMER", is_staff: false },
  } as unknown as Agent4Conversation;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

let container: HTMLDivElement;
let root: Root;
afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  fetchConversation.mockReset();
  sessionStorage.clear();
});

function showEarlier() {
  const button = [...container.querySelectorAll("button")].find((item) => item.textContent?.includes("Show earlier messages"));
  expect(button).toBeTruthy();
  act(() => button?.click());
}

function mount(node: ReactNode) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(node));
}

function screen(me: Me, c: WorkflowCase) {
  return (
    <SessionContext.Provider value={{ me, health: null }}>
      <Agent4Chat c={c} />
    </SessionContext.Provider>
  );
}

function text() {
  return container.textContent ?? "";
}

describe("Agent 4 chat view", () => {
  it("keeps a case's stored transcript off screen until the user asks for it", async () => {
    fetchConversation.mockImplementation((path) => Promise.resolve(
      conversation(String(path).includes("case-2") ? "case-2" : "case-1", String(path).includes("case-2") ? null : "what is my situation"),
    ));

    mount(screen(user("user-a"), caseOf("case-1")));
    await act(async () => {
      await Promise.resolve();
    });
    expect(text()).not.toContain("what is my situation");
    expect(text()).toContain("No messages yet");
    expect(text()).toContain("Show earlier messages");

    showEarlier();
    expect(text()).toContain("what is my situation");

    act(() => root.render(screen(user("user-a"), caseOf("case-2"))));
    await act(async () => {
      await Promise.resolve();
    });
    expect(text()).not.toContain("what is my situation");
    expect(text()).toContain("No messages yet");
    expect(text()).not.toContain("Show earlier messages");
  });

  it("starts empty after sign-out when a different user signs in", async () => {
    const first = deferred<Agent4Conversation>();
    const second = deferred<Agent4Conversation>();
    fetchConversation.mockImplementation((path) => (
      String(path).includes("case-b") ? second.promise : first.promise
    ));

    mount(screen(user("user-a"), caseOf("case-a")));
    await act(async () => {
      first.resolve(conversation("case-a", "Only user A said this"));
    });
    expect(text()).not.toContain("Only user A said this");
    expect(text()).toContain("Show earlier messages");

    beginChatSession();
    act(() => root.render(screen(user("user-b"), caseOf("case-b"))));
    expect(text()).not.toContain("Only user A said this");
    expect(text()).toContain("Opening Agent 4");

    await act(async () => {
      second.resolve(conversation("case-b", null));
    });
    expect(text()).toContain("No messages yet");
    expect(text()).not.toContain("Only user A said this");
    expect(text()).not.toContain("Show earlier messages");
  });

  it("hides the stored transcript again on the next sign-in, even for the same case", async () => {
    fetchConversation.mockResolvedValue(conversation("case-1", "Kept on the case"));

    mount(screen(user("user-a"), caseOf("case-1")));
    await act(async () => {
      await Promise.resolve();
    });
    expect(text()).not.toContain("Kept on the case");
    showEarlier();
    expect(text()).toContain("Kept on the case");

    beginChatSession();
    act(() => root.render(screen(user("user-a"), caseOf("case-1"))));
    await act(async () => {
      await Promise.resolve();
    });
    expect(text()).not.toContain("Kept on the case");
    expect(text()).toContain("Show earlier messages");
  });

  it("starts empty again when a new case flow forgets that case's visible thread", async () => {
    fetchConversation.mockResolvedValue(conversation("case-1", "Kept on the case"));
    mount(screen(user("user-a"), caseOf("case-1")));
    await act(async () => {
      await Promise.resolve();
    });
    showEarlier();
    expect(text()).toContain("Kept on the case");

    forgetChatCase("case-1");
    act(() => root.unmount());
    container.remove();
    mount(screen(user("user-a"), caseOf("case-1")));
    await act(async () => {
      await Promise.resolve();
    });
    expect(text()).not.toContain("Kept on the case");
    expect(text()).toContain("Show earlier messages");
  });
});
