import { money, statusLabel, statusTone } from "../labels";
import type { CaseSummary } from "../types";
import { Card, Empty, ErrorText, Pill, Working, useLoad } from "../ui";

export default function MyCases() {
  const { data, error, loading } = useLoad<CaseSummary[]>("/workflow/cases");
  if (loading) return <Working text="Loading your cases" />;
  return (
    <div className="mx-auto max-w-[760px] space-y-3">
      <h1 className="text-2xl">My cases</h1>
      <ErrorText error={error} />
      {data?.length === 0 && <Empty>No cases yet. Start one from New case.</Empty>}
      {data?.map((c) => <CaseRow key={c.case_id} c={c} />)}
    </div>
  );
}

export function CaseRow({ c }: { c: CaseSummary }) {
  return (
    <a href={`#/cases/${c.case_id}`} className="block">
      <Card className="hover:ring-ocean">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="font-semibold">{c.invoice_number ?? "No invoice number"} · {c.vendor_name ?? "Unknown vendor"}</span>
          <span className="flex items-center gap-2">
            <span className="font-semibold">{money(c.amount, c.currency)}</span>
            <Pill tone={statusTone(c.status)}>{statusLabel(c.status)}</Pill>
          </span>
        </div>
        {(c.why || c.next_action) && (
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{c.next_action || c.why}</p>
        )}
      </Card>
    </a>
  );
}
