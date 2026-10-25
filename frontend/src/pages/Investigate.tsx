import { useState } from "react";
import { api, ApiError } from "../api";
import { money } from "../labels";
import type { WorkflowCase } from "../types";
import { Button, Card, ErrorText, Pill, Working, useLoad } from "../ui";
import { BankPill } from "./CustomerDashboard";

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
}

/**
 * "Why has my payment not arrived?" - a customer asks about one of their OWN
 * failed, missing or stuck payments. Agent 4 investigates through the bank's
 * restricted tools; the customer never sees the ledger, and only their own
 * payment requests are listed.
 */
export default function Investigate() {
  const { data, error, loading } = useLoad<{ bank_payments: BankPayment[]; cases: { case_id: string; invoice_number: string | null; status: string }[] }>("/auth/me");
  const [chosen, setChosen] = useState<string>("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, any> | null>(null);
  const [exception, setException] = useState<Record<string, any> | null>(null);

  if (loading) return <Working text="Loading your payments" />;
  const payments = data?.bank_payments ?? [];

  const ask = async () => {
    setFormError(null);
    setException(null);
    const text = message.trim();
    if (!text) return setFormError("Describe the problem, for example: “my payment for INV-… has not arrived”.");
    setBusy(true);
    try {
      const created = await api<WorkflowCase>("/workflow/cases/message", { method: "POST", json: { message: text } });
      setResult(created as unknown as Record<string, any>);
      // If the case ends on an exception, fetch Agent 4's own report so the
      // customer sees the structured diagnosis rather than a raw case dump.
      if (created.reports?.some((r) => r.agent === "agent_4")) {
        setException(created as unknown as Record<string, any>);
      }
    } catch (e) {
      setFormError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <h1 className="text-2xl">Agent investigation</h1>
      <p className="text-sm">
        Ask Agent 4 about a payment that failed, is stuck, or never arrived. It reads only evidence
        for your own case, and it never retries a payment: a retry needs a fresh human approval.
      </p>
      <ErrorText error={error} />

      <Card>
        <h2 className="mb-2 text-base">Your payments</h2>
        {payments.length === 0 ? (
          <p className="text-sm">No payments yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr>
                  {["Invoice", "To", "Amount", "Bank status", "Reference"].map((h) => (
                    <th key={h} className="py-2 text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {payments.map((p) => (
                  <tr key={p.payment_id} className="border-t border-sky/40">
                    <td className="py-2 font-medium">{p.invoice_number ?? "—"}</td>
                    <td>{p.payee ?? "—"}</td>
                    <td>{money(p.amount, p.currency)}</td>
                    <td><BankPill status={p.status} /></td>
                    <td className="font-mono text-xs">{p.settlement_reference ?? p.failure_code ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card>
        <h2 className="text-base">Describe the problem</h2>
        <label className="mt-2 block text-sm font-semibold">
          Which payment? (optional)
          <select
            value={chosen}
            onChange={(e) => {
              const p = payments.find((x) => x.payment_id === e.target.value);
              setChosen(e.target.value);
              if (p) setMessage(`My payment for invoice ${p.invoice_number} of ${p.amount} ${p.currency} to ${p.payee} has not arrived. Please investigate.`);
            }}
            className="mt-1 w-full rounded-xl border border-sky px-3 py-2 font-normal dark:bg-[#07142f]"
          >
            <option value="">Choose one of your payments</option>
            {payments.map((p) => (
              <option key={p.payment_id} value={p.payment_id}>
                {p.invoice_number} · {money(p.amount, p.currency)} · {p.status}
              </option>
            ))}
          </select>
        </label>
        <label className="mt-3 block text-sm font-semibold">
          What happened?
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            rows={4}
            className="mt-1 w-full rounded-xl border border-sky px-3 py-2 font-normal dark:bg-[#07142f]"
          />
        </label>
        <div className="mt-3 space-y-3">
          <ErrorText error={formError} />
          <Button onClick={ask} disabled={busy}>{busy ? "Agent 4 is investigating…" : "Ask Agent 4"}</Button>
        </div>
      </Card>

      {result && (
        <Card>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-base">Agent 4's answer</h2>
            <Pill tone="info">Case {String(result.case_id ?? "").slice(0, 8)}</Pill>
          </div>
          <p className="mt-2 text-base">{result.customer_response?.answer ?? result.outcome ?? "The case has been recorded."}</p>
          {Array.isArray(result.customer_response?.next_steps) && result.customer_response.next_steps.length > 0 && (
            <>
              <h3 className="mt-3 text-sm font-semibold">Next steps</h3>
              <ul className="mt-1 list-disc pl-5 text-sm">
                {result.customer_response.next_steps.map((s: string) => <li key={s}>{s}</li>)}
              </ul>
            </>
          )}
          <p className="mt-3 text-sm">
            <a href={`#/cases/${result.case_id}`} className="text-ocean underline dark:text-sky">Open the full case</a>
          </p>
        </Card>
      )}

      {exception && (
        <Card>
          <h2 className="text-base">Exception details</h2>
          {exception.reports.filter((r: any) => r.agent === "agent_4").map((r: any) => (
            <div key={r.summary} className="mt-2 text-sm">
              <p>{r.summary}</p>
              {r.data?.customer_explanation && <p className="mt-1">{r.data.customer_explanation}</p>}
              {r.data?.failure_code && <p className="mt-1"><b>Bank code:</b> {r.data.failure_code}</p>}
              {r.data?.retry_permitted !== undefined && (
                <p className="mt-1"><b>Retry permitted automatically:</b> {r.data.retry_permitted ? "Yes" : "No"}</p>
              )}
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}
