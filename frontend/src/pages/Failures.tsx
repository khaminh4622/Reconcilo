import { money, when } from "../labels";
import { Banner, Card, Empty, ErrorText, Pill, Working, useLoad } from "../ui";

export interface FailureRow {
  source: "payment_attempt" | "ledger";
  failure_code: string | null;
  failure_reason: string | null;
  invoice_number: string | null;
  amount: string | null;
  currency: string | null;
  at: string | null;
  case_id: string | null;
  customer: { display_name: string | null; name_on_document: string | null } | null;
}

/** The backend lists each failure once per source; show it once, noting both sources. */
export function mergeFailures(rows: FailureRow[]): (FailureRow & { sources: string[] })[] {
  const merged = new Map<string, FailureRow & { sources: string[] }>();
  for (const row of rows) {
    const key = `${row.case_id ?? "no-case"}|${row.invoice_number ?? ""}`;
    const existing = merged.get(key);
    if (existing) {
      existing.sources.push(row.source);
      existing.failure_code ??= row.failure_code;
      existing.failure_reason ??= row.failure_reason;
    } else merged.set(key, { ...row, sources: [row.source] });
  }
  return [...merged.values()];
}

export default function Failures() {
  const { data, error, loading } = useLoad<{ failures: FailureRow[] }>("/admin/payment-failures");
  if (loading) return <Working text="Loading payment failures" />;
  const rows = mergeFailures(data?.failures ?? []);
  return (
    <div className="space-y-3">
      <h1 className="text-2xl">Payment failures</h1>
      <Banner>Simulated, no real money.</Banner>
      <ErrorText error={error} />
      {rows.length === 0 && !error && <Empty>No failed payments.</Empty>}
      {rows.map((f, i) => (
        <Card key={i}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold">{f.invoice_number ?? "Unknown invoice"} · {money(f.amount, f.currency)}</span>
            <Pill tone="failure">{f.failure_code ?? "No code"}</Pill>
          </div>
          <p className="mt-1 text-sm">{f.failure_reason ?? "The bank gave no reason."}</p>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            {f.customer?.display_name ?? f.customer?.name_on_document ?? "Unknown customer"} · {when(f.at)} · seen in {f.sources.join(" and ").replaceAll("_", " ")}
            {f.case_id && <> · <a className="text-ocean underline dark:text-sky" href={`#/cases/${f.case_id}`}>Open case</a></>}
          </p>
        </Card>
      ))}
    </div>
  );
}
