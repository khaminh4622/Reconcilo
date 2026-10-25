// Shapes returned by the backend (backend/workflow/schemas.py, main.py,
// workflow/admin_api.py, workflow/approvals.py). Only fields the UI reads.

export type Role = "CUSTOMER" | "BANK_ADMIN" | "DEMO_ADMIN";

export interface Account {
  account_id: string;
  email: string;
  display_name: string;
  role: Role;
  created_at: string;
}

export type CaseStatus =
  | "RECEIVED" | "ANALYZING" | "VALIDATING" | "ROUTED" | "INVESTIGATING" | "AWAITING_APPROVAL"
  | "APPROVED" | "EXECUTING" | "PENDING" | "COMPLETED" | "FAILED" | "REQUIRES_REVIEW";

export type RunStatus = "waiting" | "running" | "completed" | "blocked" | "requires_human" | "skipped";

export interface AgentRun {
  agent: string;
  agent_name: string;
  status: RunStatus;
  order: number;
  phase: string | null;
  summary: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface Finding {
  code: string;
  label: string;
  status: "passed" | "failed" | "unverified" | "info";
  detail: string;
}

export interface AgentReport {
  agent: string;
  agent_name: string;
  summary: string;
  findings: Finding[];
  next_action: string | null;
  requires_human: boolean;
  resolved: boolean;
  data: Record<string, any>;
  completed_at: string | null;
}

export interface CustomerResponse {
  case_status: string;
  outcome: string;
  headline: string;
  answer: string;
  next_steps: string[];
  payment_settled: boolean;
  missing_information: string[];
  generated_by: "deterministic" | "llm";
}

export interface ApprovalPolicy {
  required: boolean;
  eligible_roles: Role[];
  requester: { account_id: string; display_name: string | null; email: string | null } | null;
  blocked: { user: string; reason: string }[];
}

export interface AuditEntry {
  entry_id: string;
  at: string;
  actor: string;
  action: string;
  detail: string;
  actor_account_id?: string | null;
}

export interface PaymentAttempt {
  attempt_id: string;
  invoice_number: string | null;
  payee: string | null;
  amount: string | null;
  currency: string | null;
  status: "SUCCESS" | "FAILED" | "PENDING";
  provider_status: string;
  failure_code: string | null;
  failure_reason: string | null;
  provider_reference: string | null;
  settlement_reference: string | null;
  verified: boolean;
  attempted_at: string;
}

export interface Receipt {
  receipt_id: string;
  case_id: string;
  invoice_number: string | null;
  vendor_name: string | null;
  amount: string | null;
  currency: string | null;
  provider_reference: string | null;
  settlement_reference: string | null;
  approved_by: string | null;
  settled_at: string | null;
  disclaimer: string;
  verification: string[];
}

export interface WorkflowCase {
  case_id: string;
  created_at: string;
  updated_at: string;
  status: CaseStatus;
  intent: string;
  customer_name: string | null;
  vendor_name: string | null;
  invoice_number: string | null;
  amount: string | null;
  currency: string | null;
  analysis: Record<string, any> | null;
  owner_account_id: string | null;
  agent_runs: AgentRun[];
  reports: AgentReport[];
  approval: { approved_by: string; approved_at: string; note: string | null } | null;
  payment: PaymentAttempt | null;
  reconciliation: Record<string, any> | null;
  receipt: Receipt | null;
  audit: AuditEntry[];
  outcome: string | null;
  requires_human: boolean;
  pending_reason: string | null;
  customer_response: CustomerResponse | null;
  source_channel: "upload" | "message" | "demo" | null;
  approval_policy?: ApprovalPolicy | null;
  // Triage fields, written for the bank reviewer (backend summarise_for_human).
  why?: string | null;
  problems?: string[];
  next_action?: string | null;
}

export interface CaseSummary {
  case_id: string;
  created_at: string;
  updated_at: string;
  status: CaseStatus;
  intent: string;
  invoice_number: string | null;
  vendor_name: string | null;
  amount: string | null;
  currency: string | null;
  requires_human: boolean;
  why: string | null;
  next_action: string | null;
}

export interface Health {
  llm_configured: boolean;
  llm_model: string | null;
  detail: string | null;
}

// --- Agent 4 support chat (backend workflow/agent4_support.py) -------------

export type Agent4Actor = "customer" | "staff" | "agent_4";

export interface Agent4Message {
  message_id: string;
  case_id: string;
  account_id: string | null;
  actor: Agent4Actor | string;
  body: string;
  action_id: string | null;
  created_at: string;
}

export interface Agent4BankRequest {
  payment_id: string;
  case_id: string | null;
  invoice_number: string | null;
  payee: string | null;
  amount: string | null;
  currency: string | null;
  status: string;
  settlement_reference: string | null;
  failure_code: string | null;
  failure_reason: string | null;
  created_at: string | null;
  processed_at: string | null;
}

export interface Agent4Bank extends Agent4BankRequest {
  status_label: string;
  source: string;
  simulated: boolean;
  ledger_entries?: number;
  retry_blocked?: boolean;
  decisions?: { decision: string; decided_at: string; reason: string | null }[] | null;
  attempts_in_case?: { payment_id: string; status: string; amount: string }[];
}

export interface Agent4PaymentStatus {
  case_id: string;
  case_status: CaseStatus;
  invoice_number: string | null;
  vendor: string | null;
  customer_name: string | null;
  amount: string | null;
  currency: string | null;
  requires_human: boolean;
  pending_reason: string | null;
  agent_3: {
    validation_status: string | null;
    summary: string | null;
    document_type?: string | null;
    analysis_source?: string | null;
  };
  agent_1: { status: string | null; summary: string | null };
  agent_4: {
    summary: string | null;
    diagnosis: string | null;
    customer_explanation: string | null;
    retry_permitted: boolean | null;
  };
  reconciliation: Record<string, any>;
  payment_request_id: string | null;
  payment_history: Agent4BankRequest[];
  bank: Agent4Bank | null;
  bank_status_label: string;
  settlement_verified: boolean;
  has_bank_record: boolean;
  retry_blocked: boolean;
  can_request_payment: boolean;
  reason_not_requestable: string | null;
  next_steps: string[];
  simulated: boolean;
  payment_attempts?: {
    attempt_id: string;
    status: string;
    provider_status: string;
    failure_code: string | null;
    failure_reason: string | null;
    settlement_reference: string | null;
    verified: boolean;
  }[];
}

export interface Agent4SuggestedAction {
  id: string;
  label: string;
  message: string;
  available: boolean;
  reason: string | null;
}

export interface Agent4Escalation {
  escalated: boolean;
  reason: string;
  at: string | null;
  already_escalated: boolean;
}

export interface Agent4Conversation {
  case_id: string;
  invoice_number: string | null;
  case_status: CaseStatus;
  requires_human: boolean;
  payment_status: Agent4PaymentStatus;
  messages: Agent4Message[];
  suggested_actions: Agent4SuggestedAction[];
  next_steps: string[];
  escalation: Agent4Escalation | null;
  viewer: { account_id: string | null; role: Role | null; is_staff: boolean } | null;
}

export interface Agent4Reply {
  case_id: string;
  case_status: CaseStatus;
  requires_human: boolean;
  customer_message: Agent4Message;
  agent_message: Agent4Message;
  payment_status: Agent4PaymentStatus;
  suggested_actions: Agent4SuggestedAction[];
  next_steps: string[];
  escalation: Agent4Escalation | null;
  tools_used: string[];
  conversation?: Agent4Conversation;
}

export interface Agent4PaymentIntake {
  case: WorkflowCase;
  source_case_id: string;
  payment_request: {
    payment_id: string;
    status: string;
    amount: string;
    currency: string;
    invoice_number: string | null;
  } | null;
  message: string;
  conversation?: Agent4Conversation;
}
