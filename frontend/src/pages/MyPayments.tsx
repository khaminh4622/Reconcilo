import { useMemo, useState } from "react";
import type { Me } from "../App";
import { money, when } from "../labels";
import { Banner, Card, Empty, ErrorText, PageIntro, Pill, Working, useLoad } from "../ui";
import { BankPill, paymentState, type PaymentState } from "./bankStatus";

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
}

const FILTERS: { id: "all" | PaymentState; label: string }[] = [
  { id: "all", label: "All" },
  { id: "pending", label: "Pending" },
  { id: "approved", label: "Approved" },
  { id: "declined", label: "Declined" },
  { id: "failed", label: "Failed" },
  { id: "settled", label: "Settled" },
];

export function paymentPill(status: string, verified: boolean) {
  if (status === "SUCCESS" && verified) return <Pill tone="confirmed">Settled</Pill>;
  if (status === "FAILED") return <Pill tone="failure">Failed</Pill>;
  if (status === "SUCCESS") return <Pill tone="attention">Approved, not confirmed</Pill>;
  return <Pill tone="attention">Pending</Pill>;
}

export default function MyPayments() {
  const { data, error, loading } = useLoad<Me>("/auth/me");
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["id"]>("all");
  const bank = (data?.bank_payments ?? []) as BankPayment[];
  const attempts = data?.payment_history ?? [];
  const visible = useMemo(
    () => bank.filter((row) => filter === "all" || paymentState(row.status) === filter),
    [bank, filter],
  );
  const counts = useMemo(() => {
    const tally: Record<string, number> = { all: bank.length };
    for (const row of bank) {
      const state = paymentState(row.status);
      tally[state] = (tally[state] ?? 0) + 1;
    }
    return tally;
  }, [bank]);

  if (loading && !data) return <Working text="Loading your payments" />;

  return (
    <div className="rise space-y-5">
      <PageIntro
        kicker="Payments"
        title="Payment history"
        lede="These rows belong to your account. Pending waits for a bank administrator. Settled is the only state in which the simulated bank has finished both sides of the transfer."
      />
      <Banner>Simulated, no real money.</Banner>
      <ErrorText error={error} />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {FILTERS.filter((item) => item.id !== "all").map((item) => (
          <button key={item.id} type="button" onClick={() => setFilter(item.id)} className="text-left">
            <Card className={filter === item.id ? "ring-2 ring-ocean" : ""}>
              <p className="text-[0.7rem] font-bold uppercase tracking-[0.14em] text-slate-500">{item.label}</p>
              <p className="mt-2 font-display text-3xl text-navy">{counts[item.id] ?? 0}</p>
            </Card>
          </button>
        ))}
      </div>

      <Card className="overflow-hidden p-0">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-5 py-4">
          <h2 className="text-base font-semibold">Bank records</h2>
          <div role="group" aria-label="Payment state" className="flex flex-wrap gap-1.5">
            {FILTERS.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-pressed={filter === item.id}
                onClick={() => setFilter(item.id)}
                className="rounded-full px-3 py-1 text-xs font-semibold ring-1 ring-line aria-pressed:bg-navy aria-pressed:text-white"
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
        {bank.length === 0 ? (
          <div className="p-5"><Empty>No payments yet. <a href="#/new" className="font-semibold text-ocean hover:underline">Start a case</a>.</Empty></div>
        ) : visible.length === 0 ? (
          <div className="p-5"><Empty>No payments in this state.</Empty></div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data">
              <thead>
                <tr>
                  {["Invoice", "Payee", "Amount", "State", "Reference", "When", ""].map((heading) => <th key={heading || "open"}>{heading}</th>)}
                </tr>
              </thead>
              <tbody>
                {visible.map((payment) => (
                  <tr key={payment.payment_id}>
                    <td className="font-semibold">{payment.invoice_number ?? "—"}</td>
                    <td>{payment.payee ?? "—"}</td>
                    <td className="whitespace-nowrap">{money(payment.amount, payment.currency)}</td>
                    <td><BankPill status={payment.status} /></td>
                    <td className="font-mono text-xs">{payment.settlement_reference ?? payment.failure_code ?? "—"}</td>
                    <td className="whitespace-nowrap text-slate-500">{when(payment.processed_at ?? payment.created_at)}</td>
                    <td>
                      {payment.case_id && <a href={`#/cases/${payment.case_id}`} className="text-sm font-semibold text-ocean hover:underline">Case</a>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {visible.some((payment) => payment.failure_reason) && (
          <ul className="space-y-2 border-t border-line px-5 py-4 text-sm">
            {visible.filter((payment) => payment.failure_reason).map((payment) => (
              <li key={`${payment.payment_id}-reason`}>
                <b>{payment.invoice_number ?? "Payment"}:</b> {payment.failure_reason}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {attempts.length > 0 && (
        <Card>
          <h2 className="text-base font-semibold">Recorded payment attempts</h2>
          <p className="mt-1 text-sm text-slate-600">From the payment record tied to your cases. A success is settled only when it is also verified.</p>
          <ul className="mt-3 space-y-2">
            {attempts.map((attempt) => {
              const receipt = data?.receipts.find((item) => item.case_id === attempt.case_id);
              return (
                <li key={attempt.attempt_id}>
                  <a href={`#/cases/${attempt.case_id}`} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-foam px-3 py-3 hover:ring-1 hover:ring-ocean">
                    <span>
                      <span className="font-semibold">{attempt.invoice_number ?? "Invoice"} · {money(attempt.amount, attempt.currency)}</span>
                      <span className="mt-0.5 block text-xs text-slate-500">
                        Reference {attempt.reference ?? "—"} · {attempt.settled_at ? `Settled ${when(attempt.settled_at)}` : "Not settled"}
                        {receipt ? ` · Receipt ${String(receipt.receipt_id).slice(0, 8)}` : ""}
                      </span>
                    </span>
                    {paymentPill(String(attempt.status), Boolean(attempt.verified))}
                  </a>
                </li>
              );
            })}
          </ul>
        </Card>
      )}
    </div>
  );
}
