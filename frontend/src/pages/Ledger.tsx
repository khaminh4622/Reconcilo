import { useState } from "react";
import { api, ApiError } from "../api";
import { money } from "../labels";
import { Banner, Button, Card, Collapse, Empty, ErrorText, Pill, Raw, RecordChip, Working, useLoad } from "../ui";

interface LedgerRow {
  entry_id: string;
  payment_reference: string | null;
  invoice_number: string | null;
  customer_name: string | null;
  vendor_name: string | null;
  amount: string;
  received_amount: string | null;
  currency: string;
  status: string;
  value_date: string;
  failure_code: string | null;
}

const STATUS_TONE: Record<string, "confirmed" | "failure" | "attention"> = { SETTLED: "confirmed", FAILED: "failure" };

export default function Ledger() {
  const [filters, setFilters] = useState({ search: "", status: "", invoice: "" });
  const [query, setQuery] = useState("");
  const { data, error, loading } = useLoad<{ disclaimer: string; transactions: LedgerRow[]; balances: Record<string, string> }>(`/bank/ledger${query}`);
  const [report, setReport] = useState<Record<string, any> | null>(null);
  const [reconcileError, setReconcileError] = useState<string | null>(null);
  const [reconciling, setReconciling] = useState(false);

  const apply = (e: React.FormEvent) => {
    e.preventDefault();
    const params = new URLSearchParams(Object.entries(filters).filter(([, v]) => v));
    setQuery(params.toString() ? `?${params}` : "");
  };
  const reconcile = async () => {
    setReconciling(true);
    setReconcileError(null);
    try {
      setReport(await api("/bank/reconcile", { method: "POST" }));
    } catch (e) {
      setReconcileError((e as ApiError).message);
    } finally {
      setReconciling(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl">Bank ledger</h1>
        <Button onClick={reconcile} disabled={reconciling}>{reconciling ? "Reconciling…" : "Reconcile now"}</Button>
      </div>
      <Banner>Simulated. {data?.disclaimer ?? "No real money."}</Banner>
      <ErrorText error={reconcileError} />
      {report && (
        <Card>
          <p className="text-sm"><b>Reconciliation:</b> {report.summary || `${report.matched} matched, ${report.unmatched_bank} unmatched bank rows.`}</p>
          <Collapse title="Reconciliation details"><Raw value={report} /></Collapse>
        </Card>
      )}
      <form onSubmit={apply} className="flex flex-wrap gap-2">
        {(["search", "invoice"] as const).map((field) => (
          <input key={field} placeholder={field === "search" ? "Search" : "Invoice number"} value={filters[field]} aria-label={field}
            onChange={(e) => setFilters({ ...filters, [field]: e.target.value })}
            className="rounded-xl border border-sky px-3 py-2 text-sm dark:bg-[#07142f]" />
        ))}
        <select aria-label="Status" value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}
          className="rounded-xl border border-sky px-3 py-2 text-sm dark:bg-[#07142f]">
          <option value="">Any status</option>
          {["SETTLED", "PENDING", "PARTIAL", "FAILED"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <Button type="submit" kind="ghost">Filter</Button>
      </form>
      <ErrorText error={error} />
      {loading ? <Working text="Loading the ledger" /> : (
        <Card className="overflow-x-auto">
          {data?.transactions.length === 0 && <Empty>No transactions match.</Empty>}
          <table className="w-full text-left text-sm">
            <thead><tr><th className="py-2">Entry</th><th>Invoice</th><th>Customer</th><th>Amount</th><th>Status</th><th>Date</th></tr></thead>
            <tbody>
              {data?.transactions.map((r) => (
                <tr key={r.entry_id} className="border-t border-sky/40">
                  <td className="py-2"><RecordChip label={r.entry_id} load={() => api(`/bank/ledger/${encodeURIComponent(r.entry_id)}`)} /></td>
                  <td>{r.invoice_number ?? "—"}</td>
                  <td>{r.customer_name ?? "—"}</td>
                  <td>{money(r.received_amount ?? r.amount, r.currency)}</td>
                  <td><Pill tone={STATUS_TONE[r.status] ?? "attention"}>{r.status}{r.failure_code ? ` · ${r.failure_code}` : ""}</Pill></td>
                  <td>{r.value_date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
