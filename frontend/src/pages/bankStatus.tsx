import { Pill } from "../ui";

/** One label per stored bank status. Never inferred in the browser. */
export const BANK_LABEL: Record<string, [string, "confirmed" | "attention" | "failure" | "info" | "neutral"]> = {
  PENDING_APPROVAL: ["Pending", "attention"],
  APPROVED: ["Approved", "info"],
  PROCESSING: ["Processing", "info"],
  SETTLED: ["Settled", "confirmed"],
  REJECTED: ["Declined", "failure"],
  FAILED: ["Failed", "failure"],
  UNCERTAIN: ["Outcome unknown", "attention"],
  CANCELLED: ["Cancelled", "neutral"],
};

export const BankPill = ({ status }: { status: string }) => {
  const [text, tone] = BANK_LABEL[status] ?? [status, "neutral"];
  return <Pill tone={tone}>{text}</Pill>;
};

export type PaymentState = "pending" | "approved" | "declined" | "failed" | "settled" | "other";

export function paymentState(status: string): PaymentState {
  if (status === "PENDING_APPROVAL") return "pending";
  if (status === "APPROVED" || status === "PROCESSING") return "approved";
  if (status === "REJECTED" || status === "CANCELLED") return "declined";
  if (status === "FAILED" || status === "UNCERTAIN") return "failed";
  if (status === "SETTLED") return "settled";
  return "other";
}
