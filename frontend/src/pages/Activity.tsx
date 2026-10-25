import { useState } from "react";
import { agentName, when } from "../labels";
import { Card, Empty, ErrorText, Working, useLoad } from "../ui";

interface Entry {
  at: string;
  case_id: string;
  invoice_number: string | null;
  customer: { display_name: string | null; name_on_document: string | null };
  actor: string;
  action: string;
  detail: string;
}

export default function Activity() {
  const [limit, setLimit] = useState(50);
  const { data, error, loading } = useLoad<{ activity: Entry[] }>(`/admin/activity?limit=${limit}`);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <h1 className="text-2xl">Agent activity</h1>
        <label className="text-sm font-semibold">
          Show newest{" "}
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))} className="rounded-lg border border-sky px-2 py-1 dark:bg-[#07142f]">
            {[1, 10, 50, 100, 250, 500].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
      </div>
      <ErrorText error={error} />
      {loading ? <Working text="Loading activity" /> : (
        <Card>
          {data?.activity.length === 0 && <Empty>No activity yet.</Empty>}
          <ol className="space-y-3">
            {data?.activity.map((e, i) => (
              <li key={i} className="border-l-2 border-sky pl-3 text-sm">
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  {when(e.at)} · {agentName(e.actor)} · {e.action.replaceAll("_", " ")} · {e.customer.display_name ?? e.customer.name_on_document ?? "—"}
                </span>
                <p>{e.detail}</p>
                <a className="text-xs text-ocean underline dark:text-sky" href={`#/cases/${e.case_id}`}>{e.invoice_number ?? "Open case"}</a>
              </li>
            ))}
          </ol>
        </Card>
      )}
    </div>
  );
}
