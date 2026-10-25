import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "../App";
import { api } from "../api";
import { money, statusLabel, statusTone, when } from "../labels";
import type {
  Agent4Conversation,
  Agent4Escalation,
  Agent4Message,
  Agent4PaymentIntake,
  Agent4PaymentStatus,
  Agent4Reply,
  Agent4SuggestedAction,
  WorkflowCase,
} from "../types";
import { Button, Card, ErrorText, Pill, Working } from "../ui";

const MAX_MESSAGE = 4000;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}

export default function Agent4Chat({ c }: { c: WorkflowCase }) {
  const { me } = useSession();
  const isStaff = Boolean(me && me.account.role !== "CUSTOMER");
  const accountId = me?.account.account_id ?? null;
  // The transcript on screen belongs to one signed-in account and one case.
  // Changing either one drops the previous view before the next load arrives.
  const scopeKey = `${accountId ?? ""}:${c.case_id}`;
  const path = `/workflow/cases/${encodeURIComponent(c.case_id)}/agent4/conversation`;
  const [loadError, setLoadError] = useState<{ key: string; message: string } | null>(null);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const [messages, setMessages] = useState<Agent4Message[]>([]);
  const [status, setStatus] = useState<Agent4PaymentStatus | null>(null);
  const [actions, setActions] = useState<Agent4SuggestedAction[]>([]);
  const [nextSteps, setNextSteps] = useState<string[]>([]);
  const [escalation, setEscalation] = useState<Agent4Escalation | null>(null);
  const [draft, setDraft] = useState("");
  const [draftScope, setDraftScope] = useState(scopeKey);
  const [sending, setSending] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [newCase, setNewCase] = useState<WorkflowCase | null>(null);
  const bottom = useRef<HTMLDivElement | null>(null);
  const ready = loadedKey === scopeKey;

  if (draftScope !== scopeKey) {
    setDraftScope(scopeKey);
    setDraft("");
    setSendError(null);
    setNotice(null);
    setNewCase(null);
    setSending(false);
    setRequesting(false);
  }

  useEffect(() => {
    let live = true;
    setLoadedKey(null);
    setMessages([]);
    setStatus(null);
    setActions([]);
    setNextSteps([]);
    setEscalation(null);
    setLoadError(null);
    api<Agent4Conversation>(path)
      .then((conversation) => {
        if (!live) return;
        applyConversation(conversation);
        setLoadedKey(scopeKey);
      })
      .catch((error: unknown) => live && setLoadError({ key: scopeKey, message: errorMessage(error) }));
    return () => {
      live = false;
    };
    // scopeKey already includes the case id and the signed-in account.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, scopeKey]);

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: "nearest" });
  }, [messages.length, sending, requesting]);

  function applyConversation(conversation: Agent4Conversation) {
    setMessages(conversation.messages ?? []);
    setStatus(conversation.payment_status ?? null);
    setActions(conversation.suggested_actions ?? []);
    setNextSteps(conversation.next_steps ?? conversation.payment_status?.next_steps ?? []);
    setEscalation(conversation.escalation ?? null);
  }

  async function send(message: string, actionId?: string) {
    const text = message.trim();
    if (!text || sending) return;
    const temporary: Agent4Message = {
      message_id: `pending-${Date.now()}`,
      case_id: c.case_id,
      account_id: me?.account.account_id ?? null,
      actor: isStaff ? "staff" : "customer",
      body: text,
      action_id: actionId ?? null,
      created_at: new Date().toISOString(),
    };
    setMessages((current) => [...current, temporary]);
    setDraft("");
    setSendError(null);
    setNotice(null);
    setSending(true);
    try {
      const reply = await api<Agent4Reply>(path, {
        method: "POST",
        json: { message: text, action_id: actionId ?? null },
      });
      if (reply.conversation) {
        applyConversation(reply.conversation);
      } else {
        setMessages((current) => [
          ...current.filter((item) => item.message_id !== temporary.message_id),
          reply.customer_message,
          reply.agent_message,
        ]);
        setStatus(reply.payment_status);
        setActions(reply.suggested_actions);
        setNextSteps(reply.next_steps);
        setEscalation(reply.escalation ?? null);
      }
      if (reply.escalation?.escalated) {
        setNotice(
          "Agent 4 escalated this case to human review. No payment was approved or retried.",
        );
      }
    } catch (error) {
      setMessages((current) => current.filter((item) => item.message_id !== temporary.message_id));
      setDraft(text);
      setSendError(errorMessage(error));
    } finally {
      setSending(false);
    }
  }

  async function requestPayment() {
    if (requesting) return;
    setRequesting(true);
    setSendError(null);
    setNotice(null);
    try {
      const intake = await api<Agent4PaymentIntake>(
        `/workflow/cases/${encodeURIComponent(c.case_id)}/agent4/request-payment`,
        { method: "POST" },
      );
      setNewCase(intake.case);
      setNotice(intake.message);
      if (intake.conversation) applyConversation(intake.conversation);
    } catch (error) {
      setSendError(errorMessage(error));
    } finally {
      setRequesting(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void send(draft);
  }

  const caseStatus = status?.case_status ?? c.status;
  const needsHuman =
    Boolean(status?.requires_human) || Boolean(c.requires_human) || Boolean(escalation?.escalated);
  const agent4Finding =
    status?.agent_4?.customer_explanation ||
    status?.agent_4?.summary ||
    [...c.reports].reverse().find((report) => report.agent === "agent_4")?.summary ||
    null;
  const bank = status?.bank ?? null;

  return (
    <Card className="scroll-mt-4">
      <section id="agent4-chat" aria-labelledby="agent4-chat-title">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-xs font-bold uppercase tracking-[0.16em] text-ocean">AI support</p>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <h2 id="agent4-chat-title" className="text-base font-semibold">Talk to Agent 4</h2>
          <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-semibold text-emerald-950">
            <span className="h-2 w-2 rounded-full bg-emerald-500" aria-hidden />
            Active support assistant
          </span>
          <Pill tone={statusTone(caseStatus)}>{statusLabel(caseStatus)}</Pill>
        </div>
        <p className="mt-1 text-sm text-slate-600">
          Agent 4 can explain this payment, check the Mock Bank records for this case, ask for
          missing details, and guide you to a verified next step. It cannot approve a payment, move
          money, or retry a failed one.
        </p>

        {!ready ? (
          loadError?.key === scopeKey ? (
            <div className="mt-3">
              <ErrorText error={loadError.message} />
            </div>
          ) : (
            <Working text="Opening Agent 4" />
          )
        ) : (
          <>
            <Agent4Findings
              c={c}
              status={status}
              finding={agent4Finding}
              bank={bank}
              needsHuman={needsHuman}
              escalation={escalation}
            />

            {nextSteps.length > 0 && (
              <div className="mt-3">
                <h3 className="text-sm font-semibold">Suggested next steps</h3>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">
                  {nextSteps.map((step) => <li key={step}>{step}</li>)}
                </ul>
              </div>
            )}

            <div
              className="mt-4 max-h-[28rem] space-y-3 overflow-y-auto rounded-2xl bg-[#eef3f4] p-4 ring-1 ring-line"
              role="log"
              aria-live="polite"
              aria-label="Conversation with Agent 4"
            >
              {messages.length === 0 && !sending && (
                <p className="py-6 text-center text-sm text-slate-500 dark:text-slate-400">
                  No messages yet. Ask Agent 4 about this payment or pick a suggested action below.
                </p>
              )}
              {messages.map((message) => <Bubble key={message.message_id} message={message} />)}
              {sending && (
                <div role="status" className="flex items-center gap-2 px-1 text-sm text-slate-600">
                  <span className="flex gap-1" aria-hidden>
                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ocean" />
                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ocean [animation-delay:120ms]" />
                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ocean [animation-delay:240ms]" />
                  </span>
                  Agent 4 is checking this case&apos;s records and the bank evidence…
                </div>
              )}
              <div ref={bottom} />
            </div>

            {actions.length > 0 && (
              <div className="mt-3" role="group" aria-label="Suggested actions">
                <div className="flex flex-wrap gap-2">
                  {actions.map((action) => (
                    <button
                      key={action.id}
                      type="button"
                      onClick={() => void send(action.message, action.id)}
                      disabled={!action.available || sending}
                      title={action.reason ?? undefined}
                      className="rounded-full bg-white px-3 py-1.5 text-left text-xs font-semibold text-ocean ring-1 ring-ocean/50 hover:bg-teal-50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {action.label}
                    </button>
                  ))}
                </div>
                {actions.some((action) => !action.available && action.reason) && (
                  <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
                    {actions.filter((action) => !action.available && action.reason).map((action) => `${action.label}: ${action.reason}`).join(" ")}
                  </p>
                )}
              </div>
            )}

            {status?.can_request_payment && (
              <div className="mt-3 rounded-xl border border-dashed border-ocean p-3 dark:border-sky/60">
                <p className="text-sm font-semibold">Start a verified payment request</p>
                <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">
                  This prepares a new request through Agent 3&apos;s verified invoice facts and
                  Agent 1&apos;s duplicate check. It goes to the Mock Bank as PENDING_APPROVAL and
                  still needs a bank administrator&apos;s approval. Nothing is paid automatically.
                </p>
                <Button
                  type="button"
                  onClick={() => void requestPayment()}
                  disabled={requesting || sending}
                  className="mt-2"
                >
                  {requesting ? "Preparing the request…" : "Prepare new payment request"}
                </Button>
              </div>
            )}

            {notice && (
              <p role="status" className="mt-3 rounded-xl bg-sky/30 px-3 py-2 text-sm text-navy dark:bg-ocean/25 dark:text-sky">
                {notice}
                {newCase && (
                  <>
                    {" "}
                    <a href={`#/cases/${encodeURIComponent(newCase.case_id)}`} className="font-semibold underline">
                      Open the new case ({newCase.status.replaceAll("_", " ").toLowerCase()})
                    </a>
                    . The original case&apos;s conversation is unchanged.
                  </>
                )}
              </p>
            )}

            <form onSubmit={submit} className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-end">
              <label htmlFor="agent4-message" className="sr-only">Message Agent 4</label>
              <textarea
                id="agent4-message"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void send(draft);
                  }
                }}
                rows={2}
                maxLength={MAX_MESSAGE}
                placeholder={isStaff ? "Reply to the customer as bank staff…" : "Ask Agent 4 about this payment…"}
                className="field min-h-[3.25rem] flex-1 resize-y"
              />
              <Button type="submit" disabled={!draft.trim() || sending} className="sm:mb-0.5">
                {sending ? "Sending…" : "Send"}
              </Button>
            </form>
            <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
              {isStaff ? "You are replying as authorized bank staff. " : ""}
              Messages are stored with this case and remain after refresh or sign-in. Agent 4 reads
              only this customer&apos;s case-scoped records; responses are simulated and no real money
              moves.
            </p>
            {sendError && <div className="mt-2"><ErrorText error={sendError} /></div>}
          </>
        )}
      </section>
    </Card>
  );
}

function Agent4Findings({
  c, status, finding, bank, needsHuman, escalation,
}: {
  c: WorkflowCase;
  status: Agent4PaymentStatus | null;
  finding: string | null;
  bank: Agent4PaymentStatus["bank"];
  needsHuman: boolean;
  escalation: Agent4Escalation | null;
}) {
  const invoice = status?.invoice_number ?? c.invoice_number ?? "—";
  const amount = money(status?.amount ?? c.amount, status?.currency ?? c.currency);
  const verified = Boolean(status?.settlement_verified);
  return (
    <div className="mt-3 space-y-3">
      <div className="rounded-xl bg-foam p-3 dark:bg-[#07142f]">
        <h3 className="text-sm font-semibold">Agent 4&apos;s current findings</h3>
        <p className="mt-1 text-sm">
          {finding ?? "Agent 4 has not recorded a finding for this case yet."}
        </p>
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
          <Fact label="Invoice" value={String(invoice)} />
          <Fact label="Amount" value={amount} />
          <Fact label="Bank status" value={status?.bank_status_label ?? "No payment record found"} />
          <Fact label="Confirmed" value={verified ? "Yes, matching bank entries" : "No, not verified"} />
        </dl>
        {bank?.failure_code || bank?.failure_reason ? (
          <p className="mt-2 text-sm">
            <b>Bank reason:</b>{" "}
            {[bank.failure_code, bank.failure_reason].filter(Boolean).join(" — ")}
          </p>
        ) : null}
        {bank?.status === "PENDING_APPROVAL" && (
          <p className="mt-2 text-sm">No money has moved yet; a bank administrator must decide.</p>
        )}
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          Simulated Mock Bank evidence, read through case-scoped tools. Agent 4 cannot approve or
          move money.
        </p>
      </div>

      {needsHuman && (
        <div
          role="status"
          className="rounded-xl bg-amber-50 p-3 text-sm ring-1 ring-amber-300 dark:bg-amber-900/30 dark:ring-amber-700"
        >
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone="attention">Case status: {statusLabel(status?.case_status ?? c.status)}</Pill>
            <b>{escalation?.escalated ? "Escalated to human review" : "A person needs to review this case"}</b>
          </div>
          <p className="mt-1">
            {escalation?.reason || status?.pending_reason || c.pending_reason || c.outcome ||
              "No reason was recorded."}
          </p>
          <p className="mt-1 text-xs">No payment was approved, processed, or retried.</p>
        </div>
      )}
    </div>
  );
}

function Bubble({ message }: { message: Agent4Message }) {
  const fromAgent = message.actor === "agent_4";
  const label = fromAgent ? "Agent 4" : message.actor === "staff" ? "Bank staff" : "You";
  return (
    <div className={`flex ${fromAgent ? "justify-start" : "justify-end"}`}>
      <div
        className={`max-w-[88%] rounded-2xl px-3.5 py-2.5 text-sm shadow-sm ${
          fromAgent
            ? "bg-white text-ink ring-1 ring-line"
            : "bg-navy text-white"
        }`}
      >
        <p className={`mb-1 text-[11px] font-bold uppercase tracking-wide ${fromAgent ? "text-ocean" : "text-teal-100"}`}>
          {label}
        </p>
        <p className="whitespace-pre-wrap break-words">{message.body}</p>
        <p className={`mt-1 text-[10px] ${fromAgent ? "text-slate-500" : "text-sky"}`}>
          {when(message.created_at)}
        </p>
      </div>
    </div>
  );
}

const Fact = ({ label, value }: { label: string; value: string }) => (
  <div>
    <dt className="text-xs text-slate-500 dark:text-slate-400">{label}</dt>
    <dd className="font-semibold">{value}</dd>
  </div>
);
