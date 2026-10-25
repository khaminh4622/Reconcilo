import { useEffect } from "react";
import { useSession } from "../App";
import { money, statusLabel, statusTone, when } from "../labels";
import { Banner, Card, Empty, ErrorText, Pill, Working, useLoad } from "../ui";

interface BankPayment {
  payment_id: string;
  case_id: string | null;
  invoice_number: string | null;
  payee: string | null;
  amount: string;
  currency: string;
  status: string;
  settlement_reference: string | null;
  failure_code: string | null;
  failure_reason: string | null;
  created_at: string;
  processed_at: string | null;
  case_status: string | null;
}

interface BankAccount {
  account_id: string;
  account_number_masked: string;
  currency: string;
  balance: string;
  status: string;
}

interface Dashboard {
  account: { display_name: string };
  cases: { case_id: string; invoice_number: string | null; vendor_name: string | null; status: string; amount: string | null; currency: string | null; created_at: string }[];
  bank_payments: BankPayment[];
  bank_account: BankAccount | null;
}

/** One label per stored bank status. Never inferred in the browser. */
export const BANK_LABEL: Record<string, [string, "confirmed" | "attention" | "failure" | "info" | "neutral"]> = {
  PENDING_APPROVAL: ["Bank reviewing payment", "attention"],
  APPROVED: ["Approved, awaiting bank", "info"],
  PROCESSING: ["Bank processing", "info"],
  SETTLED: ["Settled (simulated)", "confirmed"],
  REJECTED: ["Rejected", "failure"],
  FAILED: ["Failed", "failure"],
  UNCERTAIN: ["Outcome unknown", "attention"],
  CANCELLED: ["Cancelled", "neutral"],
};

interface InvoiceRow {
  canonical_id: string;
  case_id: string;
  invoice_number: string | null;
  vendor_name: string | null;
  amount: string | null;
  currency: string | null;
  case_status: string;
  bank_status: string | null;
  created_at: string;
  updated_at: string;
  upload_count: number;
}

export const BankPill = ({ status }: { status: string }) => {
  const [text, tone] = BANK_LABEL[status] ?? [status, "neutral"];
  return <Pill tone={tone}>{text}</Pill>;
};

export default function CustomerDashboard() {
  const { me } = useSession();
  const dashboard = useLoad<Dashboard>("/auth/me");
  const invoicesFeed = useLoad<InvoiceRow[]>("/workflow/invoices");
  // A customer can leave the dashboard open while the bank administrator
  // decides. Refresh the customer-owned read models so the settled balance,
  // timeline and invoice status update without a manual reload.
  useEffect(() => {
    const timer = window.setInterval(() => {
      dashboard.reload();
      invoicesFeed.reload();
    }, 8_000);
    return () => window.clearInterval(timer);
  }, [dashboard.reload, invoicesFeed.reload]);

  const { data, error, loading } = dashboard;
  if (loading && !data) return <Working text="Loading your dashboard" />;
  const payments = currentPayments(data?.bank_payments ?? []);
  const invoices = invoicesFeed.data ?? [];
  const settled = payments.filter((p) => p.status === "SETTLED");
  const attention = payments.filter((p) => ["FAILED", "REJECTED", "UNCERTAIN", "PENDING_APPROVAL"].includes(p.status));

  return (
    <div className="space-y-3">
      <h1 className="text-2xl">Welcome, {me?.account.display_name ?? data?.account.display_name}</h1>
      <Banner>
        Simulated banking environment. Balances and transfers are fictional; no real money moves.
      </Banner>
      <ErrorText error={error ?? invoicesFeed.error} />

      <div className="grid gap-3 sm:grid-cols-3">
        <Card>
          <h2 className="text-sm text-slate-500 dark:text-slate-400">My simulated account</h2>
          {data?.bank_account ? (
            <>
              <p className="mt-1 font-display text-2xl font-bold">
                {money(data.bank_account.balance, data.bank_account.currency)}
              </p>
              <p className="text-sm">{data.bank_account.account_number_masked} · {data.bank_account.status}</p>
            </>
          ) : (
            <p className="mt-2 text-sm">
              No simulated account yet. Your invoices can still be uploaded; ask a bank admin to onboard
              you before a payment can settle.
            </p>
          )}
        </Card>
        <Card>
          <h2 className="text-sm text-slate-500 dark:text-slate-400">Settled payments</h2>
          <p className="mt-1 font-display text-2xl font-bold">{settled.length}</p>
          <p className="text-sm">{attention.length} need attention</p>
        </Card>
        <Card>
          <h2 className="text-sm text-slate-500 dark:text-slate-400">Invoices on file</h2>
          <p className="mt-1 font-display text-2xl font-bold">{invoices.length}</p>
          <a href="#/new" className="text-sm text-ocean underline dark:text-sky">Upload an invoice</a>
        </Card>
      </div>

      <Card>
        <h2 className="mb-2 text-base">Payment timeline</h2>
        {payments.length === 0 ? (
          <Empty>No payments yet. Upload an invoice to start one.</Empty>
        ) : (
          <ul className="space-y-2">
            {payments.slice(0, 8).map((p) => (
              <li key={p.payment_id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-foam px-3 py-2 dark:bg-[#07142f]">
                <span className="text-sm">
                  <b>{p.invoice_number ?? "Invoice"}</b> · {money(p.amount, p.currency)} to {p.payee ?? "vendor"}
                </span>
                <span className="flex items-center gap-2">
                  <BankPill status={p.status} />
                  {p.case_id && <a href={`#/cases/${p.case_id}`} className="text-sm text-ocean underline dark:text-sky">Open case</a>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <h2 className="mb-2 text-base">Recent invoices</h2>
        {invoices.length === 0 ? <Empty>Nothing uploaded yet.</Empty> : (
          <ul className="space-y-1 text-sm">
            {invoices.slice(0, 5).map((invoice) => (
              <li key={invoice.canonical_id} className="flex flex-wrap items-center justify-between gap-2">
                <a href={`#/cases/${invoice.case_id}`} className="text-ocean underline dark:text-sky">
                  {invoice.invoice_number ?? "No invoice number"} · {invoice.vendor_name ?? "vendor"}
                </a>
                <span className="flex items-center gap-2">
                  <span>{money(invoice.amount, invoice.currency)}</span>
                  {invoice.bank_status
                    ? <BankPill status={invoice.bank_status} />
                    : <Pill tone={statusTone(invoice.case_status as any)}>{statusLabel(invoice.case_status as any)}</Pill>}
                  <span className="text-xs text-slate-500 dark:text-slate-400">{when(invoice.created_at)}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

/** Current payment state for the compact dashboard, not the permanent ledger.
 * The bank retains every row; this only avoids presenting legacy duplicate
 * upload/request records as separate current invoices. */
export function currentPayments(rows: BankPayment[]): BankPayment[] {
  const current = new Map<string, BankPayment>();
  for (const row of rows) {
    const key = [row.invoice_number ?? "", row.payee ?? "", row.currency ?? ""].map((part) => part.trim().toLocaleLowerCase()).join("|");
    const previous = current.get(key);
    const rowTime = Date.parse(row.processed_at ?? row.created_at) || 0;
    const previousTime = Date.parse(previous?.processed_at ?? previous?.created_at ?? "") || 0;
    if (!previous || rowTime >= previousTime) current.set(key, row);
  }
  return [...current.values()];
}
