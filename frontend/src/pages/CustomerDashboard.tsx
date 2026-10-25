import { useEffect, type ReactNode } from "react";
import { useSession } from "../App";
import { money, statusLabel, statusTone, when } from "../labels";
import { BankPill } from "./bankStatus";
import { Banner, Card, Empty, ErrorText, Metric, PageIntro, Pill, Working, useLoad } from "../ui";

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

export { BankPill } from "./bankStatus";

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
  const pending = payments.filter((p) => p.status === "PENDING_APPROVAL");
  const outstanding = invoices.filter((invoice) => invoice.bank_status !== "SETTLED");
  const name = me?.account.display_name ?? data?.account.display_name ?? "there";
  const activity = recentActivity(invoices, payments);

  return (
    <div className="rise space-y-5">
      <PageIntro
        kicker="Customer portal"
        title={`Good to see you, ${name}`}
        lede="Balances, invoices and payment states below are read from your account. Simulated environment — no real money moves."
        actions={<a href="#/new" className="rounded-xl bg-ocean px-4 py-2 text-sm font-semibold text-white hover:bg-navy">Upload an invoice</a>}
      />
      <Banner>Simulated banking environment. Balances and transfers are fictional; no real money moves.</Banner>
      <ErrorText error={error ?? invoicesFeed.error} />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric
          label="Available simulated balance"
          value={data?.bank_account ? money(data.bank_account.balance, data.bank_account.currency) : "—"}
          hint={data?.bank_account
            ? `${data.bank_account.account_number_masked} · ${data.bank_account.status}`
            : "No simulated account yet. Invoices can still be uploaded."}
        />
        <Metric
          label="Outstanding invoices"
          value={String(outstanding.length)}
          hint={invoicesFeed.loading && !invoicesFeed.data
            ? "Loading invoices…"
            : outstanding.length === 0
              ? "Every invoice on file is settled, or none are uploaded."
              : totalsHint(outstanding)}
        />
        <Metric
          label="Pending approvals"
          value={String(pending.length)}
          hint={pending.length === 0 ? "Nothing is waiting on a bank administrator." : "A person at the bank still has to decide."}
        />
        <Metric
          label="Settled payments"
          value={String(settled.length)}
          hint={settled.length === 0 ? "No simulated settlement on this account yet." : "Matched and marked settled by the bank."}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-[1.3fr_0.7fr]">
        <Card>
          <div className="mb-3 flex items-center justify-between gap-2">
            <h2 className="text-base font-semibold">Recent activity</h2>
            <a href="#/payments" className="text-sm font-semibold text-ocean hover:underline">Payments</a>
          </div>
          {activity.length === 0 ? (
            <Empty>No activity yet. Upload an invoice to start a case.</Empty>
          ) : (
            <ol className="space-y-2">
              {activity.map((item) => (
                <li key={item.key} className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-foam px-3 py-3">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold">{item.title}</p>
                    <p className="text-xs text-slate-500">{item.detail}</p>
                  </div>
                  <span className="flex items-center gap-2">
                    {item.badge}
                    {item.href && <a href={item.href} className="text-sm font-semibold text-ocean hover:underline">Open</a>}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </Card>
        <Card>
          <h2 className="text-base font-semibold">How a payment moves</h2>
          <ol className="mt-3 space-y-3 text-sm">
            {[
              ["Agent 3", "Reads the invoice and checks the figures."],
              ["Agent 1", "Looks for a duplicate or an earlier payment."],
              ["Bank desk", "A bank administrator approves or declines."],
              ["Agents 5, 2, 4", "Execution, reconciliation, then exceptions if needed."],
            ].map(([title, body], index) => (
              <li key={title} className="flex gap-3">
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-navy text-xs font-bold text-white">{index + 1}</span>
                <span><b>{title}.</b> {body}</span>
              </li>
            ))}
          </ol>
          <p className="mt-4 text-xs leading-5 text-slate-500">An agent never approves its own payment. The bank decision stays with a person.</p>
        </Card>
      </div>
    </div>
  );
}

function totalsHint(invoices: InvoiceRow[]): string {
  const byCurrency = new Map<string, number>();
  let unpriced = 0;
  for (const invoice of invoices) {
    const amount = Number(invoice.amount);
    if (!Number.isFinite(amount)) {
      unpriced += 1;
      continue;
    }
    const currency = invoice.currency || "—";
    byCurrency.set(currency, (byCurrency.get(currency) ?? 0) + amount);
  }
  const parts = [...byCurrency.entries()].map(([currency, amount]) => money(amount.toFixed(2), currency));
  if (parts.length === 0) return `${invoices.length} invoice${invoices.length === 1 ? "" : "s"} without a recorded amount.`;
  return `Not settled: ${parts.join(" · ")}${unpriced ? ` · ${unpriced} without an amount` : ""}`;
}

function recentActivity(invoices: InvoiceRow[], payments: BankPayment[]) {
  const items: { key: string; at: string; title: string; detail: string; href: string | null; badge: ReactNode }[] = [];
  for (const invoice of invoices) {
    items.push({
      key: `invoice-${invoice.canonical_id}`,
      at: invoice.updated_at || invoice.created_at,
      title: `${invoice.invoice_number ?? "Invoice"} · ${invoice.vendor_name ?? "vendor"}`,
      detail: `Invoice updated ${when(invoice.updated_at || invoice.created_at)}`,
      href: `#/cases/${invoice.case_id}`,
      badge: invoice.bank_status
        ? <BankPill status={invoice.bank_status} />
        : <Pill tone={statusTone(invoice.case_status as never)}>{statusLabel(invoice.case_status as never)}</Pill>,
    });
  }
  for (const payment of payments) {
    items.push({
      key: `pay-${payment.payment_id}`,
      at: payment.processed_at || payment.created_at,
      title: `${payment.invoice_number ?? "Payment"} · ${money(payment.amount, payment.currency)}`,
      detail: `To ${payment.payee ?? "vendor"} · ${when(payment.processed_at || payment.created_at)}`,
      href: payment.case_id ? `#/cases/${payment.case_id}` : null,
      badge: <BankPill status={payment.status} />,
    });
  }
  return items.sort((a, b) => (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0)).slice(0, 6);
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
