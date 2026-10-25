import { money, statusLabel, statusTone } from "../labels";
import { Card, Empty, ErrorText, Pill, Working, useLoad } from "../ui";
import { BankPill } from "./CustomerDashboard";

interface QueueItem {
  case_id: string;
  created_at: string;
  requester: { account_id: string; display_name: string | null; email: string | null } | null;
  invoice_number: string | null;
  vendor: string | null;
  amount: string | null;
  currency: string | null;
  agent1: { status: string | null; summary: string; needs_human_judgement: boolean } | null;
}

export interface BankRequest {
  payment_id: string;
  case_id: string | null;
  invoice_id: string | null;
  amount: string;
  currency: string;
  status: string;
  payee: string | null;
}

export default function Approvals() {
  const { data, error, loading } = useLoad<{ count: number; approvals: QueueItem[] }>("/workflow/approvals");
  // The bank's own request status, so the queue never says something the
  // bank's records do not say.
  const bank = useLoad<{ requests: BankRequest[] }>("/bank-admin/requests");
  if (loading) return <Working text="Loading the approval queue" />;
  return (
    <ApprovalsList
      items={data?.approvals ?? []}
      error={error ?? bank.error}
      bankRequests={bank.data?.requests}
      count={data?.count}
    />
  );
}

/** The queue list on its own, so it can be rendered without the data hooks. */
export function ApprovalsList({
  items,
  error,
  bankRequests,
  count,
}: {
  items: QueueItem[];
  error: string | null;
  bankRequests?: BankRequest[];
  count?: number;
}) {
  const byCase = new Map<string, BankRequest>();
  for (const r of bankRequests ?? []) {
    if (r.case_id) byCase.set(r.case_id, r);
  }
  return (
    <div className="space-y-3">
      <h1 className="text-2xl">Approval queue</h1>
      <p className="text-sm">
        Payment runs only after a bank administrator authorises the exact invoice, payee, amount and
        currency — and never the person who requested it. Open <a href="#/bank" className="underline">Bank Admin</a>
        to make the recorded Approve or Decline decision and process the simulated bank result.
      </p>
      <ErrorText error={error} />
      {items.length === 0 && !error && <Empty>Nothing is waiting for approval.</Empty>}
      {items.map((a) => (
        <a key={a.case_id} href="#/bank" className="block">
          <Card className="hover:ring-ocean">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold">{a.invoice_number ?? "No invoice number"} · {a.vendor ?? "Unknown vendor"}</span>
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{money(a.amount, a.currency)}</span>
                {byCase.get(a.case_id) && <BankPill status={byCase.get(a.case_id)!.status} />}
              </span>
            </div>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
              Requested by {a.requester?.display_name ?? a.requester?.email ?? "unknown"}
            </p>
            {a.agent1 && (
              <p className="mt-2 flex flex-wrap items-center gap-2 text-sm">
                <Pill tone="check">Agent 1 · {a.agent1.status}</Pill>
                {a.agent1.needs_human_judgement && <Pill tone="attention">Needs human judgement</Pill>}
                <span>{a.agent1.summary}</span>
              </p>
            )}
          </Card>
        </a>
      ))}
      {items.length > 0 && (
        <Card>
          <h2 className="text-base">How this list is derived</h2>
          <p className="mt-1 text-sm">
            {count ?? items.length} case(s) are{" "}
            <Pill tone={statusTone("AWAITING_APPROVAL")}>{statusLabel("AWAITING_APPROVAL")}</Pill> in the
            database, and the bank status next to each comes from the Mock Bank's own payment-request
            records.
          </p>
        </Card>
      )}
    </div>
  );
}
