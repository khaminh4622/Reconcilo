import type { Me } from "../App";
import { money, when } from "../labels";
import { Banner, Card, Empty, ErrorText, Pill, Working, useLoad } from "../ui";

export function paymentPill(status: string, verified: boolean) {
  if (status === "SUCCESS" && verified) return <Pill tone="confirmed">Confirmed by the bank</Pill>;
  if (status === "FAILED") return <Pill tone="failure">Failed</Pill>;
  return <Pill tone="attention">Not confirmed yet</Pill>;
}

export default function MyPayments() {
  const { data, error, loading } = useLoad<Me>("/auth/me");
  if (loading) return <Working text="Loading your payments" />;
  const payments = data?.payment_history ?? [];
  return (
    <div className="mx-auto max-w-[760px] space-y-3">
      <h1 className="text-2xl">My payments</h1>
      <Banner>Simulated, no real money.</Banner>
      <ErrorText error={error} />
      {payments.length === 0 && <Empty>No payments yet.</Empty>}
      {payments.map((p) => {
        const receipt = data?.receipts.find((r) => r.case_id === p.case_id);
        return (
          <a key={p.attempt_id} href={`#/cases/${p.case_id}`} className="block">
            <Card className="hover:ring-ocean">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold">{p.invoice_number ?? "Invoice"} · {money(p.amount, p.currency)}</span>
                {paymentPill(p.status, p.verified)}
              </div>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                Reference {p.reference ?? "—"} · {p.settled_at ? `Settled ${when(p.settled_at)}` : "Not settled"}
                {receipt && ` · Receipt ${String(receipt.receipt_id).slice(0, 8)}`}
              </p>
            </Card>
          </a>
        );
      })}
    </div>
  );
}
