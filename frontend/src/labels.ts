import type { AgentRun, CaseStatus, RunStatus, WorkflowCase } from "./types";

export type Tone = "neutral" | "info" | "attention" | "confirmed" | "failure" | "check";

const STATUS: Record<CaseStatus, [string, Tone]> = {
  RECEIVED: ["Received", "neutral"],
  ANALYZING: ["Reading", "info"],
  VALIDATING: ["Checking", "info"],
  ROUTED: ["Routed", "info"],
  INVESTIGATING: ["Investigating", "info"],
  AWAITING_APPROVAL: ["Bank reviewing payment", "attention"],
  APPROVED: ["Approved", "info"],
  EXECUTING: ["Paying", "info"],
  PENDING: ["Payment not settled yet", "attention"],
  COMPLETED: ["Completed", "confirmed"],
  FAILED: ["Not paid", "failure"],
  REQUIRES_REVIEW: ["Needs a person", "attention"],
};

export const statusLabel = (status: CaseStatus) => STATUS[status]?.[0] ?? status;
export const statusTone = (status: CaseStatus): Tone => STATUS[status]?.[1] ?? "neutral";

// Agent 5 runs one final step whose phase says what it did.
const AGENT5_PHASE: Record<string, string> = {
  confirmation: "Confirms result",
  explanation: "Explains outcome",
  failure_report: "Explains the failure",
  rejection: "Explains the rejection",
};

const AGENT_NAMES: Record<string, string> = {
  agent_1: "Agent 1 · Duplicate check",
  agent_2: "Agent 2 · Bank reconciliation",
  agent_3: "Agent 3 · Reads your request",
  agent_4: "Agent 4 · Payment exception",
  agent_5: "Agent 5",
  payment_simulator: "Simulated payment",
  human_approval: "Bank approval",
  human_review: "Human review",
  orchestrator: "Workflow",
};

export const agentName = (agent: string) => AGENT_NAMES[agent] ?? agent;

export function stepLabel(run: Pick<AgentRun, "agent" | "phase">): string {
  if (run.agent === "agent_5") return `Agent 5 · ${AGENT5_PHASE[run.phase ?? ""] ?? "Final answer"}`;
  return agentName(run.agent);
}

const RUN: Record<RunStatus, [string, Tone]> = {
  waiting: ["Up next", "neutral"],
  running: ["Working", "info"],
  completed: ["Done", "confirmed"],
  blocked: ["Stopped", "failure"],
  requires_human: ["Needs a person", "attention"],
  skipped: ["Skipped", "neutral"],
};
export const runLabel = (status: RunStatus) => RUN[status]?.[0] ?? status;
export const runTone = (status: RunStatus): Tone => RUN[status]?.[1] ?? "neutral";

/** The visible path: the case's own agent_runs, in order, skipped steps hidden. */
export function visiblePath(runs: AgentRun[]): AgentRun[] {
  return [...runs].sort((a, b) => a.order - b.order).filter((run) => run.status !== "skipped");
}

/** One risk badge per case: the strongest signal wins, shown once. */
export function riskBadge(c: Pick<WorkflowCase, "status" | "customer_response" | "requires_human">): [string, Tone] {
  if (c.status === "FAILED") return ["Not paid", "failure"];
  if (c.customer_response?.payment_settled) return ["Payment confirmed", "confirmed"];
  if (c.status === "REQUIRES_REVIEW" || c.requires_human) return ["Needs attention", "attention"];
  return ["No issues found yet", "neutral"];
}

export const channelLabel = (channel: string | null) =>
  channel === "message" ? "Message" : channel === "demo" ? "Demo run" : channel === "upload" ? "PDF" : null;

export function money(amount: string | null | undefined, currency: string | null | undefined): string {
  if (amount == null || amount === "") return "—";
  const value = Number(amount);
  const text = Number.isFinite(value)
    ? value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : amount;
  return `${text} ${currency ?? ""}`.trim();
}

export const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "—");
