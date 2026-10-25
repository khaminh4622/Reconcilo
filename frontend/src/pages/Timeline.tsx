import { when } from "../labels";
import { useLoad } from "../ui";

interface TimelineStep {
  key: string;
  label: string;
  state: "done" | "current" | "waiting" | "failed" | "skipped";
  detail: string;
  at: string | null;
}

interface Timeline {
  case_id: string;
  status: string;
  bank_status: string | null;
  settlement_reference: string | null;
  steps: TimelineStep[];
}

const MARK: Record<TimelineStep["state"], [string, string]> = {
  done: ["✓", "bg-green-100 text-green-900 dark:bg-green-900/40 dark:text-green-200"],
  current: ["●", "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200"],
  waiting: ["○", "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400"],
  failed: ["✕", "bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200"],
  skipped: ["–", "bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500"],
};

/**
 * The payment timeline. Every state comes from the backend's stored records
 * (`/cases/{id}/timeline`); this component only chooses an icon and a colour.
 */
export default function Timeline({ caseId }: { caseId: string }) {
  const { data, error, loading } = useLoad<Timeline>(`/cases/${encodeURIComponent(caseId)}/timeline`);
  if (loading && !data) return <p className="text-sm text-slate-500 dark:text-slate-400">Loading the timeline…</p>;
  if (!data) return <p className="text-sm text-slate-500 dark:text-slate-400">{error ?? "No timeline available."}</p>;
  return (
    <div className="space-y-2">
      <ol aria-label="Payment timeline" className="space-y-2">
        {data.steps.map((step) => {
          const [mark, tone] = MARK[step.state];
          return (
            <li key={step.key} className="flex items-start gap-3">
              <span
                aria-hidden
                className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold ${tone}`}
              >
                {mark}
              </span>
              <div className="min-w-0">
                <p className="text-sm font-semibold">
                  {step.label}
                  <span className="sr-only"> — {step.state}</span>
                  {step.at && (
                    <span className="ml-2 text-xs font-normal text-slate-500 dark:text-slate-400">{when(step.at)}</span>
                  )}
                </p>
                <p className="text-sm text-slate-600 dark:text-slate-300">{step.detail}</p>
              </div>
            </li>
          );
        })}
      </ol>
      {data.settlement_reference && (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Settlement reference <span className="font-mono">{data.settlement_reference}</span> · simulated bank
        </p>
      )}
    </div>
  );
}
