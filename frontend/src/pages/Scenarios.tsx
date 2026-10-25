import { useState } from "react";
import { api, ApiError } from "../api";
import { useSession } from "../App";
import { statusLabel } from "../labels";
import type { WorkflowCase } from "../types";
import { Banner, Button, Card, Collapse, ErrorText, Pill, Raw, Working, useLoad } from "../ui";
import type { CustomerRow } from "./Customers";

interface Expected { route: string; status: string; agents: string[]; outcome: string | null }
interface Scenario { name: string; description: string; available: boolean; expected: Expected | null }

const RAN = new Set(["completed", "blocked", "requires_human"]);

/** The same measures the backend pins in tests/test_demo_scenarios.py. */
export function actualOf(c: WorkflowCase): Expected {
  return {
    route: c.intent,
    status: c.status,
    agents: [...c.agent_runs].sort((a, b) => a.order - b.order).filter((r) => RAN.has(r.status)).map((r) => r.agent),
    outcome: c.customer_response?.outcome ?? null,
  };
}

export default function Scenarios() {
  const { health } = useSession();
  const list = useLoad<{ scenarios: Scenario[] }>("/demo/scenarios");
  const customers = useLoad<{ customers: CustomerRow[] }>("/admin/customers");
  const [email, setEmail] = useState("customer.a@ghostledger.test");
  const [mode, setMode] = useState<"scripted" | "live">("scripted");
  const [running, setRunning] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, { case?: WorkflowCase; error?: string }>>({});

  const run = async (name: string) => {
    setRunning(name);
    try {
      const c = await api<WorkflowCase>(`/demo/scenarios/${encodeURIComponent(name)}/run`, { method: "POST", json: { customer_email: email, mode } });
      setResults((r) => ({ ...r, [name]: { case: c } }));
    } catch (e) {
      const err = e as ApiError;
      const message = err.status === 400 ? `Agent 3 could not read this file, so no case was opened. ${err.message}` : err.message;
      setResults((r) => ({ ...r, [name]: { error: message } }));
    } finally {
      setRunning(null);
    }
  };

  if (list.loading) return <Working text="Loading demo scenarios" />;
  return (
    <div className="space-y-4">
      <h1 className="text-2xl">Demo scenarios</h1>
      <Banner>Each file runs through the real workflow. Scripted runs use a stored reading of the PDF instead of a live AI call.</Banner>
      <ErrorText error={list.error} />
      <Card className="flex flex-wrap gap-4">
        <label className="text-sm font-semibold">
          Run as customer{" "}
          <select value={email} onChange={(e) => setEmail(e.target.value)} className="rounded-lg border border-sky px-2 py-1 dark:bg-[#07142f]">
            {(customers.data?.customers ?? [{ email } as CustomerRow]).map((c) => <option key={c.email} value={c.email}>{c.email}</option>)}
          </select>
        </label>
        <label className="text-sm font-semibold">
          Reading{" "}
          <select value={mode} onChange={(e) => setMode(e.target.value as "scripted" | "live")} className="rounded-lg border border-sky px-2 py-1 dark:bg-[#07142f]">
            <option value="scripted">Scripted</option>
            {health?.llm_configured && <option value="live">Live AI</option>}
          </select>
        </label>
      </Card>
      {list.data?.scenarios.map((s) => {
        const result = results[s.name];
        return (
          <Card key={s.name}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-base">{s.name}</h2>
              <Button onClick={() => run(s.name)} disabled={!s.available || running !== null}>
                {running === s.name ? "Running…" : "Run"}
              </Button>
            </div>
            <p className="mt-1 text-sm">{s.description}</p>
            {running === s.name && <Working text="Agent 3 is reading the document" />}
            {result?.error && <div className="mt-2"><ErrorText error={result.error} /></div>}
            {result?.case && <Comparison expected={s.expected} c={result.case} />}
            {!result && s.expected && (
              <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                Expected on a fresh database: {s.expected.route} → {s.expected.status} ({s.expected.agents.join(" → ") || "no agents"})
                {s.expected.outcome && `, answer ${s.expected.outcome}`}
              </p>
            )}
          </Card>
        );
      })}
      <DeveloperTools />
    </div>
  );
}

function Comparison({ expected, c }: { expected: Expected | null; c: WorkflowCase }) {
  const actual = actualOf(c);
  const rows: [string, string, string][] = [
    ["Route", expected?.route ?? "—", actual.route],
    ["Status", expected?.status ?? "—", actual.status],
    ["Agents", expected?.agents.join(" → ") ?? "—", actual.agents.join(" → ")],
    ["Answer", expected?.outcome ?? "—", actual.outcome ?? "—"],
  ];
  const same = expected && rows.every(([, e, a]) => e === a);
  return (
    <div className="mt-3 space-y-2">
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <Pill tone={same ? "confirmed" : "attention"}>{same ? "Matches expected" : "Differs from expected"}</Pill>
        {c.analysis?.analysis_source === "synthetic_fixture" && <Pill tone="neutral">Scripted reading, not a live AI call</Pill>}
        <a className="text-ocean underline dark:text-sky" href={`#/cases/${c.case_id}`}>Open case ({statusLabel(c.status)})</a>
      </p>
      <table className="w-full text-left text-sm">
        <thead><tr><th></th><th>Expected</th><th>Actual</th></tr></thead>
        <tbody>{rows.map(([label, e, a]) => <tr key={label} className="border-t border-sky/40"><td className="py-1 font-semibold">{label}</td><td>{e}</td><td>{a}</td></tr>)}</tbody>
      </table>
      {!same && <p className="text-xs">Outcomes can differ when the database already holds earlier runs.</p>}
    </div>
  );
}

function DeveloperTools() {
  const scenarios = useLoad<{ scenarios: { code: string; reason: string }[] }>("/developer/failure-scenarios");
  const [form, setForm] = useState({ invoice_number: "INV-DEV-1", amount: "100.00", currency: "USD", payee: "Northstar Office Supplies LLC", customer_name: "Demo Customer A", failure_code: "" });
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const simulate = async () => {
    setError(null);
    try {
      setResult(await api("/developer/simulate-failure", { method: "POST", json: { ...form, failure_code: form.failure_code || undefined } }));
    } catch (e) {
      setError((e as ApiError).message);
    }
  };
  return (
    <Collapse title="Developer tools (local only)">
      <div className="space-y-3 text-sm">
        <p>Simulate a failed payment for Agent 4 to investigate. To choose a simulated bank result for an approval, use the developer option on the case's approve panel (Success, Failed, Partial).</p>
        <ErrorText error={scenarios.error} />
        <div className="grid gap-2 sm:grid-cols-2">
          {(Object.keys(form) as (keyof typeof form)[]).filter((k) => k !== "failure_code").map((key) => (
            <label key={key} className="font-semibold">
              {key.replaceAll("_", " ")}
              <input value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                className="mt-1 w-full rounded-xl border border-sky px-3 py-2 font-normal dark:bg-[#07142f]" />
            </label>
          ))}
          <label className="font-semibold">
            failure code
            <select value={form.failure_code} onChange={(e) => setForm({ ...form, failure_code: e.target.value })}
              className="mt-1 w-full rounded-xl border border-sky px-3 py-2 font-normal dark:bg-[#07142f]">
              <option value="">Default</option>
              {scenarios.data?.scenarios.map((s) => <option key={s.code} value={s.code}>{s.code} · {s.reason}</option>)}
            </select>
          </label>
        </div>
        <ErrorText error={error} />
        <Button kind="ghost" onClick={simulate}>Simulate failure</Button>
        {result !== null && <Raw value={result} />}
      </div>
    </Collapse>
  );
}
