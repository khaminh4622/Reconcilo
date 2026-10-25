import { agentName, statusLabel } from "../labels";
import type { CaseStatus } from "../types";
import { Card, ErrorText, Working, useLoad } from "../ui";

interface OverviewData {
  cases_total: number;
  cases_by_status: Record<string, number>;
  cases_needing_a_person: number;
  payments: { settled: number; failed: number; pending: number };
  agent_runs: Record<string, number>;
}

export default function Overview() {
  const { data, error, loading } = useLoad<OverviewData>("/admin/overview");
  if (loading) return <Working text="Loading the overview" />;
  if (!data) return <ErrorText error={error} />;
  const max = Math.max(1, ...Object.values(data.agent_runs));
  return (
    <div className="space-y-4">
      <h1 className="text-2xl">Overview</h1>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Cases" value={data.cases_total} />
        <Stat label="Need a person" value={data.cases_needing_a_person} tone="text-amber-700 dark:text-amber-300" />
        <Stat label="Payments confirmed" value={data.payments.settled} tone="text-green-700 dark:text-green-300" />
        <Stat label="Payments failed" value={data.payments.failed} tone="text-red-700 dark:text-red-300" />
      </div>
      <p className="text-sm">{data.payments.pending} payment(s) not confirmed yet. Simulated, no real money.</p>
      <Card>
        <h2 className="mb-2 text-base">Cases by status</h2>
        <ul className="grid grid-cols-2 gap-1 text-sm sm:grid-cols-3">
          {Object.entries(data.cases_by_status).map(([status, n]) => (
            <li key={status}>{statusLabel(status as CaseStatus)}: <b>{n}</b></li>
          ))}
        </ul>
      </Card>
      <Card>
        <h2 className="mb-2 text-base">Runs per agent</h2>
        <ul className="space-y-2">
          {Object.entries(data.agent_runs).map(([agent, n]) => (
            <li key={agent} className="text-sm">
              <div className="flex justify-between"><span>{agentName(agent)}</span><b>{n}</b></div>
              <div className="h-2 rounded-full bg-foam dark:bg-[#07142f]">
                <div className="h-2 rounded-full bg-ocean" style={{ width: `${(n / max) * 100}%` }} />
              </div>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

const Stat = ({ label, value, tone = "" }: { label: string; value: number; tone?: string }) => (
  <Card>
    <p className="text-xs text-slate-500 dark:text-slate-400">{label}</p>
    <p className={`font-display text-3xl font-extrabold ${tone}`}>{value}</p>
  </Card>
);
