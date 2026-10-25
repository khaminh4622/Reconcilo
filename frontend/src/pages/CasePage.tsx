import { useSession } from "../App";
import { api } from "../api";
import {
  agentName, channelLabel, money, riskBadge, runLabel, runTone, statusLabel, statusTone, stepLabel, visiblePath, when,
} from "../labels";
import type { AgentReport, Finding, WorkflowCase } from "../types";
import { Card, Collapse, ErrorText, Pill, RecordChip, Working, useLoad } from "../ui";
import Agent4Chat from "./Agent4Chat";
import Timeline from "./Timeline";

const AGENT4_CASE_STATUSES = new Set<string>([
  "AWAITING_APPROVAL", "APPROVED", "PENDING", "FAILED", "REQUIRES_REVIEW",
]);

export default function CasePage({ caseId }: { caseId: string }) {
  const { data: c, error, loading } = useLoad<WorkflowCase>(`/workflow/cases/${encodeURIComponent(caseId)}`);
  if (loading && !c) return <Working text="Opening the case" />;
  if (!c) return <ErrorText error={error ?? "Case not found."} />;
  return <CaseView c={c} />;
}

export function CaseView({ c }: { c: WorkflowCase }) {
  const { me } = useSession();
  const isCustomer = me?.account.role === "CUSTOMER";
  const isBankAdmin = me?.account.role === "BANK_ADMIN";
  const [risk, riskTone] = riskBadge(c);
  const channel = channelLabel(c.source_channel);
  const scripted = c.analysis?.analysis_source === "synthetic_fixture";
  const showAgent4 =
    Boolean(c.invoice_number) &&
    (Boolean(c.payment || c.reconciliation) ||
      c.reports.some((report) => report.agent === "agent_4") ||
      AGENT4_CASE_STATUSES.has(c.status));

  return (
    <article className="rise mx-auto max-w-4xl space-y-4">
      <Answer c={c} isCustomer={isCustomer} />

      <Card>
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={statusTone(c.status)}>{statusLabel(c.status)}</Pill>
          {risk !== statusLabel(c.status) && <Pill tone={riskTone}>{risk}</Pill>}
          {scripted && <Pill tone="neutral">Scripted reading, not a live AI call</Pill>}
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
          <Fact label="Amount" value={money(c.amount, c.currency)} />
          <Fact label="Vendor" value={c.vendor_name ?? "—"} />
          <Fact label="Invoice" value={c.invoice_number ?? "—"} />
          {channel && <Fact label="Sent as" value={channel} />}
        </dl>
      </Card>

      <Path c={c} />

      <Card>
        <h2 className="mb-2 text-base">Payment timeline</h2>
        <Timeline caseId={c.case_id} />
      </Card>

      {c.status === "AWAITING_APPROVAL" &&
        (isCustomer ? <WaitingForApprover c={c} /> : isBankAdmin ? <BankAdminDecisionLink c={c} /> : <DecisionRestricted />)}

      {c.status === "APPROVED" && isBankAdmin && <BankAdminDecisionLink c={c} />}

      {c.status === "REQUIRES_REVIEW" && <ReviewPanel c={c} />}

      <PaymentSection c={c} />

      {showAgent4 && <Agent4Chat c={c} />}

      <Findings c={c} />

      <Collapse title="How did Reconcilo decide?">
        <Decision c={c} />
      </Collapse>
      <Collapse title="Full history">
        <ol className="space-y-2 text-sm">
          {c.audit.map((e) => (
            <li key={e.entry_id} className="border-l-2 border-sky pl-3">
              <span className="text-xs text-slate-500 dark:text-slate-400">{when(e.at)} · {agentName(e.actor)} · {e.action.replaceAll("_", " ")}</span>
              <p>{e.detail}</p>
            </li>
          ))}
        </ol>
      </Collapse>
      <p className="text-xs text-slate-500 dark:text-slate-400">
        Case <RecordChip label={c.case_id.slice(0, 8)} value={c} />
      </p>
    </article>
  );
}

const Fact = ({ label, value }: { label: string; value: string }) => (
  <div>
    <dt className="text-xs text-slate-500 dark:text-slate-400">{label}</dt>
    <dd className="font-semibold">{value}</dd>
  </div>
);

/** One answer, one next action. Agent 5's customer_response wins when present. */
export function Answer({ c, isCustomer }: { c: WorkflowCase; isCustomer: boolean }) {
  const response = c.customer_response;
  const latest = [...c.reports].reverse().find((r) => r.summary);
  const title = response?.headline ?? statusLabel(c.status);
  const sentence =
    response?.answer ?? c.pending_reason ?? c.why ?? latest?.summary ?? c.outcome ?? "Reconcilo is working on this case.";
  // next_action is written for the bank reviewer ("Approve … or reject it"), so
  // only admins see it; a customer's next step comes from their own panel.
  const steps = response?.next_steps ?? (!isCustomer && c.next_action ? [c.next_action] : []);
  return (
    <Card className={response?.payment_settled ? "ring-2 ring-green-500" : ""}>
      <h1 className="text-2xl">{title}</h1>
      <p className="mt-2 text-base">{sentence}</p>
      {steps.length > 0 && (
        <>
          <h2 className="mt-4 text-base">Next steps</h2>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">{steps.map((s) => <li key={s}>{s}</li>)}</ul>
        </>
      )}
    </Card>
  );
}

const STEP_ROLE: Record<string, string> = {
  agent_3: "Verification. Agent 3 reads the request and checks the figures before anyone else runs.",
  agent_1: "Duplicate detection. Agent 1 compares this invoice with earlier records.",
  human_approval: "Bank approval. A person decides. No agent can approve this payment.",
  payment_simulator: "Payment execution. The simulated bank acts only after a recorded approval.",
  agent_5: "Payment execution. Agent 5 reports the outcome. It does not approve or move money on its own.",
  agent_2: "Reconciliation. Agent 2 matches the payment against bank records.",
  agent_4: "Exception handling. Agent 4 explains what the bank recorded. A retry still needs a fresh approval.",
  human_review: "A person reviews this case before anything else happens.",
  orchestrator: "The workflow coordinator. Agents do not call each other directly.",
};

export function Path({ c }: { c: Pick<WorkflowCase, "agent_runs" | "reports" | "approval" | "payment" | "reconciliation" | "analysis" | "status" | "pending_reason"> }) {
  const runs = visiblePath(c.agent_runs);
  return (
    <Card>
      <div className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.16em] text-ocean">AI workflow</p>
          <h2 className="text-base font-semibold">Agent path</h2>
        </div>
        <p className="max-w-sm text-xs leading-5 text-slate-500">Each step is a stored run. Evidence underneath is copied from that agent's report.</p>
      </div>
      <ol className="relative space-y-0" aria-label="Workflow path">
        {runs.map((run, index) => {
          const evidence = evidenceFor(run, c);
          const live = run.status === "running" || run.status === "requires_human";
          return (
            <li key={`${run.agent}-${run.order}`} className="relative grid grid-cols-[2.25rem_minmax(0,1fr)] gap-3 pb-4">
              {index < runs.length - 1 && <span aria-hidden className="absolute left-[1.05rem] top-8 bottom-0 w-px bg-line" />}
              <span className={`relative z-10 mt-0.5 flex h-8 w-8 items-center justify-center rounded-full text-xs font-bold ${live ? "bg-ocean text-white" : run.status === "completed" ? "bg-navy text-white" : run.status === "blocked" ? "bg-red-700 text-white" : "bg-white text-slate-500 ring-1 ring-line"}`}>
                {index + 1}
              </span>
              <div className={`rounded-2xl px-4 py-3 ${run.agent === "payment_simulator" ? "border border-dashed border-ocean bg-white" : "bg-foam"}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-sm font-semibold">{stepLabel(run)}</span>
                  <Pill tone={runTone(run.status)}>{runLabel(run.status)}</Pill>
                </div>
                <p className="mt-1 text-sm text-slate-600">{STEP_ROLE[run.agent] ?? run.summary ?? "Recorded workflow step."}</p>
                {evidence.length > 0 && (
                  <ul className="mt-2 space-y-1 border-t border-line/80 pt-2 text-sm">
                    {evidence.map((line) => <li key={line}>{line}</li>)}
                  </ul>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

function evidenceFor(
  run: { agent: string; summary: string | null },
  c: Pick<WorkflowCase, "reports" | "approval" | "payment" | "reconciliation" | "analysis" | "status" | "pending_reason">,
): string[] {
  const report = [...c.reports].reverse().find((item) => item.agent === run.agent);
  const lines: string[] = [];
  const summary = report?.summary || run.summary;
  if (summary) lines.push(summary);
  if (run.agent === "agent_3") {
    const source = c.analysis?.analysis_source;
    const validation = c.analysis?.validation?.validation_status;
    if (validation) lines.push(`Validation status: ${validation}`);
    if (source) lines.push(`Analysis source: ${source}`);
  }
  if (run.agent === "agent_1" && report?.data?.status) lines.push(`Duplicate check: ${report.data.status}`);
  if (run.agent === "human_approval") {
    if (c.approval) lines.push(`Approved by ${c.approval.approved_by}${c.approval.note ? ` — ${c.approval.note}` : ""}`);
    else if (c.status === "AWAITING_APPROVAL") lines.push(c.pending_reason || "Waiting for a bank administrator.");
  }
  if (run.agent === "agent_2" && c.reconciliation) {
    const verdict = c.reconciliation.verdict;
    if (verdict) lines.push(`Reconciliation: ${verdict}`);
    if (c.reconciliation.summary && c.reconciliation.summary !== summary) lines.push(String(c.reconciliation.summary));
  }
  if ((run.agent === "agent_5" || run.agent === "payment_simulator") && c.payment) {
    lines.push(`Bank result: ${c.payment.provider_status}${c.payment.verified ? " · confirmed" : " · not confirmed"}`);
    if (c.payment.failure_reason) lines.push(c.payment.failure_reason);
  }
  if (run.agent === "agent_4" && report?.data) {
    if (report.data.customer_explanation) lines.push(String(report.data.customer_explanation));
    if (report.data.failure_code) lines.push(`Bank code: ${report.data.failure_code}`);
  }
  return [...new Set(lines)].slice(0, 4);
}

export function WaitingForApprover({ c }: { c: WorkflowCase }) {
  const policy = c.approval_policy;
  return (
    <Card>
      <h2 className="text-base">Bank reviewing payment</h2>
      <p className="mt-1 text-sm">
        You requested this, so you can't approve or decline it. A bank administrator makes the decision in
        the Bank Admin portal; payment only runs after they approve.
      </p>
      {policy && policy.blocked.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-sm text-slate-600 dark:text-slate-300">
          {policy.blocked.map((b) => <li key={b.user}>{b.reason}</li>)}
        </ul>
      )}
    </Card>
  );
}

/** Human decisions happen on the dedicated bank surface, never in a customer case view. */
function BankAdminDecisionLink({ c }: { c: WorkflowCase }) {
  return (
    <Card className="ring-2 ring-ocean">
      <h2 className="text-base">Bank decision required</h2>
      <p className="mt-1 text-sm">
        Approve or decline this payment from Bank Admin. The portal records the signed-in bank
        administrator, checks the Agent 3 and Agent 1 evidence, and then sends exactly one authorised
        instruction to the Mock Bank.
      </p>
      <a href="#/bank" className="mt-3 inline-block rounded-xl bg-ocean px-4 py-2 text-sm font-semibold text-white hover:bg-navy">
        Open Bank Admin
      </a>
      <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">Case {c.case_id.slice(0, 8)}</p>
    </Card>
  );
}

function DecisionRestricted() {
  return (
    <Card>
      <h2 className="text-base">Bank decision required</h2>
      <p className="mt-1 text-sm">Only a Bank Admin account can inspect or decide this payment.</p>
    </Card>
  );
}

function ReviewPanel({ c }: { c: WorkflowCase }) {
  const missing = c.customer_response?.missing_information ?? [];
  return (
    <section className="rounded-2xl bg-amber-50 p-5 ring-1 ring-amber-300 dark:bg-amber-900/30 dark:ring-amber-700">
      <h2 className="text-base">A person needs to look at this</h2>
      {c.pending_reason && <p className="mt-1 text-sm">{c.pending_reason}</p>}
      {missing.length > 0 && <p className="mt-2 text-sm"><b>Missing:</b> {missing.join(", ")}</p>}
    </section>
  );
}

function PaymentSection({ c }: { c: WorkflowCase }) {
  const agent4 = [...c.reports].reverse().find((r) => r.agent === "agent_4");
  if (!c.payment && !c.reconciliation && !agent4) return null;
  const p = c.payment;
  const rec = c.reconciliation;
  const a4 = agent4?.data ?? {};
  return (
    <Card>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-base">Payment</h2>
        <Pill tone="neutral">Simulated, no real money</Pill>
      </div>
      {p && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
          <Fact label="Amount" value={money(p.amount, p.currency)} />
          <Fact label="Bank result" value={p.provider_status} />
          <Fact label="Confirmed" value={p.verified ? "Yes, by Agent 2" : "No"} />
          {p.provider_reference && <div><dt className="text-xs text-slate-500">Reference</dt><dd><RecordChip label={p.provider_reference} value={p} /></dd></div>}
          {p.failure_code && <Fact label="Failure" value={`${p.failure_code}: ${p.failure_reason ?? ""}`} />}
        </dl>
      )}
      {c.receipt && (
        <p className="mt-3 text-sm">
          Receipt <RecordChip label={c.receipt.receipt_id.slice(0, 8)}
            load={() => api(`/workflow/cases/${c.case_id}/receipt`)} />
        </p>
      )}
      {rec && (
        <p className="mt-3 text-sm">
          <b>Reconciliation:</b> {String(rec.verdict ?? "—")}{rec.summary ? ` · ${rec.summary}` : ""}
        </p>
      )}
      {agent4 && (
        <div className="mt-3 rounded-xl bg-foam p-3 text-sm dark:bg-[#07142f]">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm">Agent 4 · Support assistant</h3>
            <Pill tone="check">Active</Pill>
            <span className="text-xs text-slate-500 dark:text-slate-400">Ask a question in “Talk to Agent 4” below.</span>
          </div>
          {a4.customer_explanation && <p className="mt-1">{a4.customer_explanation}</p>}
          {a4.diagnosis && <p className="mt-1"><b>Diagnosis:</b> {a4.diagnosis}</p>}
          {a4.failure_code && <p className="mt-1"><b>Bank code:</b> {a4.failure_code}</p>}
          {a4.recommended_resolution && <p className="mt-1"><b>Recommended:</b> {a4.recommended_resolution}</p>}
          {a4.retry_permitted !== undefined && <p className="mt-1"><b>Retry permitted:</b> {a4.retry_permitted ? "Yes" : "No"}</p>}
        </div>
      )}
      <p className="mt-3 text-sm font-semibold">A retry needs a fresh approval.</p>
    </Card>
  );
}

const FINDING_TONE: Record<Finding["status"], "check" | "failure" | "attention" | "neutral"> = {
  passed: "check", failed: "failure", unverified: "attention", info: "neutral",
};
const FINDING_TEXT: Record<Finding["status"], string> = {
  passed: "Passed", failed: "Failed", unverified: "Not verified", info: "Info",
};

function Findings({ c }: { c: WorkflowCase }) {
  const reports = c.reports.filter((r) => r.agent !== "agent_5");
  if (reports.length === 0) return null;
  return (
    <Collapse title="What the agents found">
      <div className="space-y-3">{reports.map((r, i) => <ReportCard key={i} r={r} caseId={c.case_id} />)}</div>
    </Collapse>
  );
}

function ReportCard({ r, caseId }: { r: AgentReport; caseId: string }) {
  return (
    <div className="rounded-xl bg-foam p-3 dark:bg-[#07142f]">
      <h3 className="text-sm">{agentName(r.agent)}</h3>
      <p className="mt-1 text-sm">{r.summary}</p>
      {r.agent === "agent_1" && (
        <p className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <Pill tone="check">{String(r.data.status)}</Pill>
          <span>{r.requires_human ? "Needs human judgement" : "No human judgement needed"}</span>
          <RecordChip label="Evidence" load={() => api(`/workflow/cases/${caseId}/investigation`)} />
        </p>
      )}
      {r.findings.length > 0 && (
        <ul className="mt-2 space-y-1">
          {r.findings.map((f, i) => (
            <li key={i} className={`text-sm ${f.status === "unverified" ? "border-l-4 border-dashed border-amber-500 pl-2" : ""}`}>
              <Pill tone={FINDING_TONE[f.status]}>{FINDING_TEXT[f.status]}</Pill> <b>{f.label}:</b> {f.detail}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Decision({ c }: { c: WorkflowCase }) {
  const routing = c.analysis?.routing;
  const checks: any[] = c.analysis?.validation?.validation_findings ?? [];
  return (
    <div className="space-y-3 text-sm">
      {routing && (
        <p><b>Agent 3 routed this to</b> {routing.agent_name ?? routing.recommended_agent}. {routing.reason}</p>
      )}
      <p className="text-xs text-slate-500 dark:text-slate-400">
        Python checks (teal) are deterministic. Agent 3 source: {String(c.analysis?.analysis_source ?? "—")}.
      </p>
      {c.problems && c.problems.length > 0 && (
        <div>
          <b>Problems found</b>
          <ul className="mt-1 list-disc pl-5">{c.problems.map((p) => <li key={p}>{p}</li>)}</ul>
        </div>
      )}
      {checks.length > 0 && (
        <ul className="space-y-2">
          {checks.map((check, i) => (
            <li key={i}>
              <Pill tone={check.status === "passed" ? "check" : check.status === "failed" ? "failure" : check.status === "unverified" ? "attention" : "neutral"}>
                {check.status.replaceAll("_", " ")}
              </Pill>{" "}
              <b>{check.label}.</b> {check.explanation}
              {(check.expected || check.reported) && (
                <span className="block text-xs text-slate-600 dark:text-slate-300">
                  Expected {check.expected ?? "—"} · reported {check.reported ?? "—"}{check.variance ? ` · difference ${check.variance}` : ""}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
