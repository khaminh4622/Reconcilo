import { useState } from "react";
import { api, ApiError } from "../api";
import { money, when } from "../labels";
import { Banner, Button, Card, Collapse, Empty, ErrorText, Pill, Raw, Working, useLoad } from "../ui";
import { BankPill } from "./CustomerDashboard";

interface Account {
  account_id: string;
  owner_id: string;
  owner_name: string | null;
  owner_email: string | null;
  owner_role: string | null;
  owner_type: string;
  account_number_masked: string;
  currency: string;
  balance: string;
  status: string;
}

interface Request {
  payment_id: string;
  transaction_id?: string;
  ledger_entry_ids?: string[];
  case_id: string | null;
  invoice_id: string | null;
  cid?: string;
  payee: string | null;
  amount: string;
  currency: string;
  status: string;
  approved_by: string | null;
  approved_at: string | null;
  settlement_reference: string | null;
  failure_code: string | null;
  failure_reason: string | null;
  created_at: string;
  processed_at: string | null;
  source: { account_number_masked: string; balance: string; owner_name: string | null; status: string } | null;
  destination: { account_number_masked: string; balance: string; owner_name: string | null; status: string } | null;
  requesting_account: { display_name: string; email: string } | null;
  customer_name?: string | null;
  vendor_name?: string | null;
  agent_checks?: {
    agent_3: AgentCheck;
    agent_1: AgentCheck;
  };
}

interface AgentCheck {
  status: string;
  passed: boolean;
  summary: string;
  validation_status?: string | null;
  investigation_status?: string | null;
  previous_payment_status?: string | null;
}

interface Overview {
  disclaimer: string;
  total_balances: Record<string, string>;
  summary: Record<string, any>;
  accounts: Account[];
}

interface Queue {
  disclaimer: string;
  count: number;
  summary: Record<string, any>;
  requests: Request[];
}

const TABS = [
  ["queue", "Waitlist bank statement"],
  ["accounts", "Bank accounts"],
  ["balances", "Customer balances"],
  ["transactions", "Transaction history"],
  ["audit", "Audit log"],
] as const;

export default function BankAdmin() {
  const [tab, setTab] = useState<(typeof TABS)[number][0]>("queue");
  const overview = useLoad<Overview>("/bank-admin/overview");
  const queue = useLoad<Queue>("/bank-admin/requests");
  const audit = useLoad<{ logs: Record<string, any>[] }>(tab === "audit" ? "/bank-admin/audit" : null);
  const transactions = useLoad<{ transactions: Request[] }>(tab === "transactions" ? "/bank-admin/transactions" : null);

  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<Record<string, any> | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [rejectionReason, setRejectionReason] = useState("");
  const pendingRequests = (queue.data?.requests ?? []).filter((request) => request.status === "PENDING_APPROVAL");

  const refresh = () => {
    queue.reload();
    overview.reload();
    transactions.reload();
  };

  const decide = async (paymentId: string, decision: "approve" | "reject") => {
    setBusy(paymentId);
    setError(null);
    setLastResult(null);
    try {
      const result = await api<Record<string, any>>(`/bank-admin/requests/${paymentId}/${decision}`, {
        method: "POST",
        json: decision === "reject" ? { reason: rejectionReason.trim() } : {},
      });
      setLastResult(result);
      setRejectingId(null);
      setRejectionReason("");
      refresh();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  const process = async (paymentId: string) => {
    setBusy(paymentId);
    setError(null);
    setLastResult(null);
    try {
      setLastResult(await api<Record<string, any>>(`/bank-admin/requests/${paymentId}/process`, { method: "POST" }));
      refresh();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl">Bank admin</h1>
        <Button kind="ghost" onClick={() => { overview.reload(); queue.reload(); }}>Refresh</Button>
      </div>
      <Banner>
        {overview.data?.disclaimer ??
          "SIMULATED BANK — NO REAL MONEY. Every account, balance and transfer on this page is fictional."}
      </Banner>
      <ErrorText error={overview.error ?? queue.error ?? error} />

      {overview.data && (
        <div className="grid gap-3 sm:grid-cols-4">
          <Card><Stat label="Total simulated funds" value={Object.entries(overview.data.total_balances).map(([c, v]) => `${v} ${c}`).join(" · ") || "0.00 USD"} /></Card>
          <Card><Stat label="Awaiting approval" value={String(overview.data.summary.pending_approval ?? 0)} /></Card>
          <Card><Stat label="Settled payments" value={String(overview.data.summary.settled ?? 0)} /></Card>
          <Card><Stat label="Failed / rejected" value={String(overview.data.summary.failed_or_rejected ?? 0)} /></Card>
        </div>
      )}

      <div role="tablist" className="flex flex-wrap gap-2" aria-label="Bank Admin categories">
        {TABS.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
            className="rounded-xl px-3 py-1.5 text-sm font-semibold ring-1 ring-ocean aria-selected:bg-ocean aria-selected:text-white">
            {label}
          </button>
        ))}
      </div>

      {tab === "queue" && <Card className="ring-2 ring-ocean" aria-label="Bank statement approval waitlist">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base">Waitlist bank statement</h2>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
              Approval waitlist — review the invoice and the Agent 3 / Agent 1 checks before making the bank decision.
            </p>
          </div>
          <Pill tone={pendingRequests.length ? "attention" : "confirmed"}>
            {pendingRequests.length} waiting for decision{pendingRequests.length === 1 ? "" : "s"}
          </Pill>
        </div>
        {queue.loading && !queue.data && <div className="mt-3"><Working text="Loading the bank statement" /></div>}
        {queue.data && pendingRequests.length === 0 && (
          <p className="mt-3 rounded-xl bg-foam px-3 py-2 text-sm dark:bg-[#07142f]">
            No payments are waiting for a bank decision.
          </p>
        )}
        <div className="mt-3 space-y-3">
          {pendingRequests.map((r) => (
            <section key={r.payment_id} className="rounded-xl bg-foam p-3 dark:bg-[#07142f]">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="font-semibold">
                    {r.invoice_id ?? "No invoice"} · {money(r.amount, r.currency)} to {r.payee ?? r.destination?.owner_name ?? "vendor"}
                  </p>
                  <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                    From {r.requesting_account?.display_name ?? r.source?.owner_name ?? "customer"} · {r.source?.account_number_masked ?? "account —"}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button onClick={() => decide(r.payment_id, "approve")} disabled={busy === r.payment_id}>
                    {busy === r.payment_id ? "Approving…" : "Approve"}
                  </Button>
                  <Button kind="danger" onClick={() => {
                    setRejectingId(rejectingId === r.payment_id ? null : r.payment_id);
                    setRejectionReason("");
                  }} disabled={busy === r.payment_id}>
                    Decline
                  </Button>
                  <Button kind="ghost" onClick={() => setDetailId(detailId === r.payment_id ? null : r.payment_id)}>
                    {detailId === r.payment_id ? "Hide details" : "View details"}
                  </Button>
                </div>
              </div>
              <div className="mt-2 flex flex-wrap gap-2 text-xs">
                <AgentCheckBadge label="Agent 3 verification" check={r.agent_checks?.agent_3} />
                <AgentCheckBadge label="Agent 1 duplicate check" check={r.agent_checks?.agent_1} />
              </div>
              {rejectingId === r.payment_id && (
                <div className="mt-3 rounded-xl bg-red-50 p-3 dark:bg-red-900/30">
                  <label className="block text-sm font-semibold">
                    Decline reason
                    <input
                      autoFocus
                      value={rejectionReason}
                      onChange={(e) => setRejectionReason(e.target.value)}
                      placeholder="Explain why this payment is being declined"
                      className="mt-1 w-full rounded-xl border border-red-300 bg-white px-3 py-2 font-normal dark:bg-[#07142f]"
                    />
                  </label>
                  <div className="mt-2 flex gap-2">
                    <Button kind="danger" onClick={() => decide(r.payment_id, "reject")} disabled={!rejectionReason.trim() || busy === r.payment_id}>
                      {busy === r.payment_id ? "Declining…" : "Confirm decline"}
                    </Button>
                    <Button kind="ghost" onClick={() => setRejectingId(null)}>Cancel</Button>
                  </div>
                </div>
              )}
              {detailId === r.payment_id && <PaymentDetails request={r} />}
            </section>
          ))}
        </div>
      </Card>}

      {lastResult && (
        <Card className={lastResult.status === "SETTLED" ? "ring-2 ring-green-500" : "ring-2 ring-amber-500"}>
          <h2 className="text-base">Bank decision: {lastResult.status}</h2>
          {lastResult.settlement_reference && (
            <p className="mt-1 text-sm">Settlement reference <b className="font-mono">{lastResult.settlement_reference}</b></p>
          )}
          {lastResult.failure_code && (
            <p className="mt-1 text-sm"><b>{lastResult.failure_code}</b>: {lastResult.failure_reason}</p>
          )}
          {lastResult.workflow_error && <p className="mt-1 text-sm text-amber-800">Workflow note: {lastResult.workflow_error}</p>}
          {lastResult.request?.case_id && (
            <p className="mt-2 text-sm">
              <a href={`#/case/${lastResult.request.case_id}`} className="text-ocean underline dark:text-sky">Open the case</a>
            </p>
          )}
        </Card>
      )}

      {tab === "queue" && (
        <>
          <Card>
            <h2 className="text-base">Human authorisation & automatic simulated settlement</h2>
            <p className="mt-1 text-sm">
              Approve records your authorisation for the exact invoice, payee, amount and currency,
              then sends it to the Mock Bank. The bank checks account status, available simulated
              funds, currency and reference, commits debit and credit ledger rows atomically, and
              triggers Agent 2 and Agent 5. The requester can never authorise their own payment.
            </p>
          </Card>
          {queue.loading && !queue.data && <Working text="Loading payment requests" />}
          {queue.data?.requests.length === 0 && <Empty>No payment requests yet.</Empty>}
          {queue.data?.requests.map((r) => (
            <Card key={r.payment_id}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold">
                  {r.invoice_id ?? "No invoice"} · {money(r.amount, r.currency)} to {r.payee ?? r.destination?.owner_name ?? "vendor"}
                </span>
                <span className="flex flex-wrap items-center gap-2">
                  <BankPill status={r.status} />
                  {r.status === "PENDING_APPROVAL" && <span className="text-xs text-slate-500 dark:text-slate-400">Decide in Bank statement above</span>}
                  {r.status === "APPROVED" && (
                    <Button onClick={() => process(r.payment_id)} disabled={busy === r.payment_id}>
                      {busy === r.payment_id ? "Processing…" : "Process at bank"}
                    </Button>
                  )}
                  {r.status !== "PENDING_APPROVAL" && (
                    <Button kind="ghost" onClick={() => setDetailId(detailId === r.payment_id ? null : r.payment_id)}>
                      {detailId === r.payment_id ? "Hide details" : "View details"}
                    </Button>
                  )}
                  {r.case_id && (
                    <a href={`#/case/${r.case_id}`} className="text-sm text-ocean underline dark:text-sky">Open case</a>
                  )}
                </span>
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
                <Fact label="Requested by" value={r.requesting_account?.display_name ?? "—"} />
                <Fact label="Payer account" value={`${r.source?.account_number_masked ?? "—"} (${r.source?.balance ?? "—"})`} />
                <Fact label="Recipient" value={`${r.destination?.account_number_masked ?? "—"} (${r.destination?.balance ?? "—"})`} />
                <Fact label="Approved by" value={r.approved_by ?? "not yet authorised"} />
                <Fact label="Settlement ref" value={r.settlement_reference ?? "—"} />
                <Fact label="Created" value={when(r.created_at)} />
                <Fact label="Processed" value={when(r.processed_at)} />
                <Fact label="Failure" value={r.failure_code ? `${r.failure_code}` : "—"} />
              </dl>
              <div className="mt-2 flex flex-wrap gap-2 text-xs">
                <AgentCheckBadge label="Agent 3 verification" check={r.agent_checks?.agent_3} />
                <AgentCheckBadge label="Agent 1 duplicate check" check={r.agent_checks?.agent_1} />
              </div>
              {r.failure_reason && <p className="mt-1 text-sm">{r.failure_reason}</p>}
              {detailId === r.payment_id && r.status !== "PENDING_APPROVAL" && <PaymentDetails request={r} />}
              <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                Payment request <span className="font-mono">{r.payment_id}</span>
              </p>
            </Card>
          ))}
        </>
      )}

      {tab === "accounts" && (
        <Card className="overflow-x-auto">
          {overview.loading && !overview.data && <Working text="Loading accounts" />}
          <table className="w-full text-left text-sm">
            <thead>
              <tr>
                {["Account ID", "Owner", "Owner ID", "Type", "Account", "Currency", "Available balance", "Status"].map((h) => (
                  <th key={h} className="py-2 text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {overview.data?.accounts.map((a) => (
                <tr key={a.account_id} className="border-t border-sky/40">
                  <td className="py-2 font-mono text-xs">{a.account_id}</td>
                  <td className="py-2">
                    <span className="font-medium">{a.owner_name ?? a.owner_id}</span>
                    {a.owner_email && <span className="block text-xs text-slate-500 dark:text-slate-400">{a.owner_email}</span>}
                  </td>
                  <td className="font-mono text-xs">{a.owner_id}</td>
                  <td>{a.owner_type}</td>
                  <td className="font-mono text-xs">{a.account_number_masked}</td>
                  <td>{a.currency}</td>
                  <td className="font-semibold">{money(a.balance, a.currency)}</td>
                  <td><Pill tone={a.status === "ACTIVE" ? "confirmed" : "failure"}>{a.status}</Pill></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {tab === "balances" && (
        <Card className="overflow-x-auto">
          <h2 className="mb-2 text-base">Current customer balances</h2>
          <table className="w-full text-left text-sm">
            <thead>
              <tr>
                {["Customer", "Account ID", "Masked account", "Currency", "Available balance", "Status"].map((h) => (
                  <th key={h} className="py-2 text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {overview.data?.accounts.filter((a) => a.owner_type === "CUSTOMER").map((a) => (
                <tr key={a.account_id} className="border-t border-sky/40">
                  <td className="py-2">{a.owner_name ?? a.owner_id}</td>
                  <td className="font-mono text-xs">{a.account_id}</td>
                  <td className="font-mono text-xs">{a.account_number_masked}</td>
                  <td>{a.currency}</td>
                  <td className="font-semibold">{money(a.balance, a.currency)}</td>
                  <td><Pill tone={a.status === "ACTIVE" ? "confirmed" : "failure"}>{a.status}</Pill></td>
                </tr>
              ))}
            </tbody>
          </table>
          {overview.data?.accounts.filter((a) => a.owner_type === "CUSTOMER").length === 0 && <Empty>No customer accounts yet.</Empty>}
        </Card>
      )}

      {tab === "transactions" && (
        <Card className="overflow-x-auto">
          {transactions.loading && !transactions.data && <Working text="Loading transactions" />}
          <table className="w-full text-left text-sm">
            <thead>
              <tr>
                {["Transaction ID", "Payment ID", "Invoice", "Customer", "Vendor", "Amount", "Currency", "Status", "Bank reference", "Timestamp"].map((h) => (
                  <th key={h} className="py-2 text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {transactions.data?.transactions.map((r) => (
                <tr key={r.payment_id} className="border-t border-sky/40">
                  <td className="py-2 font-mono text-xs">{r.transaction_id ?? "—"}</td>
                  <td className="font-mono text-xs">{r.payment_id}</td>
                  <td>{r.invoice_id ?? "—"}</td>
                  <td>{r.customer_name ?? r.source?.owner_name ?? "—"}</td>
                  <td>{r.vendor_name ?? r.destination?.owner_name ?? r.payee ?? "—"}</td>
                  <td>{money(r.amount, r.currency)}</td>
                  <td>{r.currency}</td>
                  <td><BankPill status={r.status} /></td>
                  <td className="font-mono text-xs">{r.settlement_reference ?? "—"}</td>
                  <td>{when(r.processed_at ?? r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {transactions.data?.transactions.length === 0 && <Empty>No transactions.</Empty>}
        </Card>
      )}

      {tab === "audit" && (
        <Card>
          {audit.loading && !audit.data && <Working text="Loading the audit log" />}
          {audit.data?.logs.length === 0 && <Empty>The audit log is empty.</Empty>}
          <Collapse title={`Bank audit log (${audit.data?.logs.length ?? 0})`}>
            <ol className="space-y-2 text-sm">
              {audit.data?.logs.map((log) => (
                <li key={log.log_id} className="border-l-2 border-sky pl-3">
                  <span className="text-xs text-slate-500 dark:text-slate-400">
                    {when(log.timestamp)} · {log.actor_role ?? "SYSTEM"} · {String(log.action).replaceAll("_", " ")}
                  </span>
                  <p className="font-mono text-xs">{log.resource_type} {log.resource_id}</p>
                  {log.details && <Raw value={safeJson(log.details)} />}
                </li>
              ))}
            </ol>
          </Collapse>
        </Card>
      )}
    </div>
  );
}

function PaymentDetails({ request }: { request: Request }) {
  return (
    <section className="mt-3 rounded-xl bg-foam p-3 text-sm dark:bg-[#07142f]" aria-label="Payment details">
      <h3 className="font-semibold">Payment details</h3>
      <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
        <Fact label="Payment ID" value={request.payment_id} />
        <Fact label="Case ID" value={request.case_id ?? "—"} />
        <Fact label="Invoice ID" value={request.invoice_id ?? "—"} />
        <Fact label="Customer" value={request.requesting_account?.display_name ?? request.source?.owner_name ?? "—"} />
        <Fact label="Vendor" value={request.payee ?? request.destination?.owner_name ?? "—"} />
        <Fact label="Amount" value={money(request.amount, request.currency)} />
        <Fact label="Payer" value={`${request.source?.account_number_masked ?? "—"} · ${request.source?.status ?? "—"}`} />
        <Fact label="Recipient" value={`${request.destination?.account_number_masked ?? "—"} · ${request.destination?.status ?? "—"}`} />
        <Fact label="Recorded approval" value={request.approved_by ?? "Not yet authorised"} />
        <Fact label="Approved at" value={when(request.approved_at)} />
        <Fact label="Bank reference" value={request.settlement_reference ?? "—"} />
        <Fact label="Failure" value={request.failure_code ? `${request.failure_code}${request.failure_reason ? `: ${request.failure_reason}` : ""}` : "—"} />
      </dl>
      <div className="mt-3 border-t border-sky/40 pt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Required checks</h4>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <AgentCheckDetail label="Agent 3 · invoice verification" check={request.agent_checks?.agent_3} />
          <AgentCheckDetail label="Agent 1 · duplicate & payment history" check={request.agent_checks?.agent_1} />
        </div>
      </div>
    </section>
  );
}

function AgentCheckBadge({ label, check }: { label: string; check?: AgentCheck }) {
  const text = !check ? "not available" : check.passed ? "passed" : check.status.replaceAll("_", " ").toLowerCase();
  return (
    <span className={`rounded-full px-2 py-1 font-semibold ${check?.passed ? "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200" : "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-100"}`}>
      {label}: {text}
    </span>
  );
}

function AgentCheckDetail({ label, check }: { label: string; check?: AgentCheck }) {
  return (
    <div className="rounded-lg bg-white/60 p-2 dark:bg-black/10">
      <p className="font-semibold">{label}: {check?.passed ? "Passed" : check?.status?.replaceAll("_", " ") ?? "Not available"}</p>
      <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">{check?.summary ?? "No linked workflow evidence."}</p>
      {check?.validation_status && <p className="mt-1 text-xs">Validation: {check.validation_status}</p>}
      {check?.previous_payment_status && <p className="mt-1 text-xs">Previous payment: {check.previous_payment_status.replaceAll("_", " ")}</p>}
    </div>
  );
}

const safeJson = (value: unknown) => {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
};

const Stat = ({ label, value }: { label: string; value: string }) => (
  <>
    <h2 className="text-sm text-slate-500 dark:text-slate-400">{label}</h2>
    <p className="mt-1 font-display text-xl font-bold">{value}</p>
  </>
);

const Fact = ({ label, value }: { label: string; value: string }) => (
  <div>
    <dt className="text-xs text-slate-500 dark:text-slate-400">{label}</dt>
    <dd className="font-semibold">{value}</dd>
  </div>
);
