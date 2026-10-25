import { useState } from "react";
import { api, ApiError } from "../api";
import { forgetChatCase } from "../chatSession";
import { money } from "../labels";
import type { WorkflowCase } from "../types";
import { Button, Card, Empty, ErrorText, PageIntro, Pill, Working, useLoad } from "../ui";
import { BankPill } from "./bankStatus";

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

  if (loading && !data) return <Working text="Loading your payments" />;
  const payments = data?.bank_payments ?? [];
  const cases = data?.cases ?? [];

  const ask = async () => {
    setFormError(null);
    setException(null);
    const text = message.trim();
    if (!text) return setFormError("Describe the problem, for example: “my payment for INV-… has not arrived”.");
    setBusy(true);
    try {
      const created = await api<WorkflowCase>("/workflow/cases/message", { method: "POST", json: { message: text } });
      forgetChatCase(created.case_id);
      setResult(created as unknown as Record<string, any>);
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
    <div className="rise space-y-5">
      <PageIntro
        kicker="AI support"
        title="Ask Agent 4"
        lede="Agent 4 explains a payment that failed, is stuck, or never arrived. It reads only your case, and it never retries a payment. A retry needs a fresh human approval."
      />
      <ErrorText error={error} />

      <div className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
        <Card>
          <h2 className="text-base font-semibold">Your payments</h2>
          {payments.length === 0 ? (
            <div className="mt-3"><Empty>No payments yet.</Empty></div>
          ) : (
            <ul className="mt-3 space-y-2">
              {payments.map((payment) => (
                <li key={payment.payment_id}>
                  <button
                    type="button"
                    onClick={() => {
                      setChosen(payment.payment_id);
                      setMessage(`My payment for invoice ${payment.invoice_number} of ${payment.amount} ${payment.currency} to ${payment.payee} has not arrived. Please investigate.`);
                    }}
                    className={`w-full rounded-xl px-3 py-3 text-left ring-1 ${chosen === payment.payment_id ? "bg-teal-50 ring-ocean" : "bg-foam ring-transparent hover:ring-line"}`}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="font-semibold">{payment.invoice_number ?? "Invoice"}</span>
                      <BankPill status={payment.status} />
                    </span>
                    <span className="mt-1 block text-sm text-slate-600">{money(payment.amount, payment.currency)} · {payment.payee ?? "vendor"}</span>
                    <span className="mt-1 block font-mono text-xs text-slate-500">{payment.settlement_reference ?? payment.failure_code ?? "No reference yet"}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {cases.length > 0 && (
            <div className="mt-4 border-t border-line pt-4">
              <h3 className="text-sm font-semibold">Open an existing conversation</h3>
              <ul className="mt-2 space-y-1 text-sm">
                {cases.slice(0, 6).map((item) => (
                  <li key={item.case_id}>
                    <a href={`#/cases/${item.case_id}#agent4-chat`} className="font-semibold text-ocean hover:underline">
                      {item.invoice_number ?? item.case_id.slice(0, 8)}
                    </a>
                    <span className="text-slate-500"> · {item.status.replaceAll("_", " ").toLowerCase()}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>

        <Card>
          <h2 className="text-base font-semibold">Describe the problem</h2>
          <label className="mt-3 block text-sm font-semibold">
            Which payment?
            <select
              value={chosen}
              onChange={(e) => {
                const payment = payments.find((item) => item.payment_id === e.target.value);
                setChosen(e.target.value);
                if (payment) setMessage(`My payment for invoice ${payment.invoice_number} of ${payment.amount} ${payment.currency} to ${payment.payee} has not arrived. Please investigate.`);
              }}
              className="field font-normal"
            >
              <option value="">Choose one of your payments</option>
              {payments.map((payment) => (
                <option key={payment.payment_id} value={payment.payment_id}>
                  {payment.invoice_number} · {money(payment.amount, payment.currency)} · {payment.status}
                </option>
              ))}
            </select>
          </label>
          <label className="mt-3 block text-sm font-semibold">
            What happened?
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={5}
              className="field font-normal"
              placeholder="My payment has not arrived."
            />
          </label>
          <div className="mt-3 space-y-3">
            <ErrorText error={formError} />
            <Button onClick={ask} disabled={busy}>{busy ? "Agent 4 is investigating…" : "Ask Agent 4"}</Button>
          </div>
          {busy && <div className="mt-3"><Working text="Agent 4 is reading your case" /></div>}
        </Card>
      </div>

      {result && (
        <Card>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-base font-semibold">Agent 4&apos;s answer</h2>
            <Pill tone="info">Case {String(result.case_id ?? "").slice(0, 8)}</Pill>
          </div>
          <p className="mt-2 text-base">{result.customer_response?.answer ?? result.outcome ?? "The case has been recorded."}</p>
          {Array.isArray(result.customer_response?.next_steps) && result.customer_response.next_steps.length > 0 && (
            <>
              <h3 className="mt-3 text-sm font-semibold">Next steps</h3>
              <ul className="mt-1 list-disc pl-5 text-sm">
                {result.customer_response.next_steps.map((step: string) => <li key={step}>{step}</li>)}
              </ul>
            </>
          )}
          <p className="mt-3 text-sm">
            <a href={`#/cases/${result.case_id}#agent4-chat`} className="font-semibold text-ocean hover:underline">Continue in the case conversation</a>
          </p>
        </Card>
      )}

      {exception && (
        <Card>
          <h2 className="text-base font-semibold">Exception details</h2>
          {exception.reports.filter((report: any) => report.agent === "agent_4").map((report: any) => (
            <div key={report.summary} className="mt-2 text-sm">
              <p>{report.summary}</p>
              {report.data?.customer_explanation && <p className="mt-1">{report.data.customer_explanation}</p>}
              {report.data?.failure_code && <p className="mt-1"><b>Bank code:</b> {report.data.failure_code}</p>}
              {report.data?.retry_permitted !== undefined && (
                <p className="mt-1"><b>Retry permitted automatically:</b> {report.data.retry_permitted ? "Yes" : "No"}</p>
              )}
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}
