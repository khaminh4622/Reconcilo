import { useState } from "react";
import { api, ApiError } from "../api";
import { go } from "../router";
import type { WorkflowCase } from "../types";
import { Button, Card, ErrorText, PageIntro, Working } from "../ui";

const MAX_MESSAGE = 20_000; // backend MAX_MESSAGE_CHARS default

export default function NewCase() {
  const [tab, setTab] = useState<"pdf" | "message">("pdf");
  const [file, setFile] = useState<File | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = async () => {
    setError(null);
    if (tab === "pdf" && !file) return setError("Choose a PDF first.");
    if (tab === "message" && !message.trim()) return setError("Write a message describing what you need.");
    setBusy(true);
    try {
      let created: WorkflowCase;
      if (tab === "pdf") {
        const form = new FormData();
        form.append("file", file!);
        created = await api<WorkflowCase>("/workflow/cases", { method: "POST", body: form });
      } else {
        created = await api<WorkflowCase>("/workflow/cases/message", { method: "POST", json: { message } });
      }
      go(`/cases/${created.case_id}`);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  if (busy) return <Working text="Agent 3 is reading your request. This can take a little while." />;

  return (
    <div className="rise mx-auto max-w-3xl space-y-5">
      <PageIntro
        kicker="Intake"
        title="New case"
        lede="Agent 3 is the first agent on every case. A PDF or a written request both enter the same workflow, and neither can approve a payment."
      />
      <div role="tablist" aria-label="How to send a case" className="flex w-fit gap-1 rounded-2xl bg-white p-1 shadow-sm ring-1 ring-line">
        {([["pdf", "Upload a PDF"], ["message", "Send a message"]] as const).map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
            className="rounded-xl px-4 py-2 text-sm font-semibold text-slate-600 aria-selected:bg-navy aria-selected:text-white">
            {label}
          </button>
        ))}
      </div>
      <Card>
        {tab === "pdf" ? (
          <DropPdf file={file} onFile={setFile} onError={setError} />
        ) : (
          <label className="block text-sm font-semibold">
            Your message
            <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={6} maxLength={MAX_MESSAGE}
              placeholder="For example: Please pay invoice INV-7781 from Northstar Office Supplies LLC for 1,280.00 USD."
              className="field font-normal" />
            <span className="mt-1 block text-xs font-normal text-slate-500">
              Include the invoice number, who it is from and the amount. {message.length.toLocaleString()} / 20,000
            </span>
          </label>
        )}
        <div className="mt-4 space-y-3">
          <ErrorText error={error} />
          <Button onClick={send}>Send to GhostLedger</Button>
        </div>
      </Card>
    </div>
  );
}

export function DropPdf({
  file, onFile, onError,
}: {
  file: File | null;
  onFile: (file: File) => void;
  onError: (message: string | null) => void;
}) {
  const [over, setOver] = useState(false);
  const pick = (chosen: File | undefined) => {
    if (!chosen) return;
    if (!chosen.name.toLowerCase().endsWith(".pdf")) return onError("Only PDF files can be uploaded.");
    onError(null);
    onFile(chosen);
  };
  return (
    <label
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0]); }}
      className={`flex cursor-pointer flex-col items-center gap-2 rounded-2xl border-2 border-dashed px-6 py-12 text-center transition-colors ${over ? "border-ocean bg-teal-50" : "border-sky bg-foam/60"}`}
    >
      <span className="flex h-11 w-11 items-center justify-center rounded-full bg-white text-ocean ring-1 ring-line">PDF</span>
      <span className="font-semibold">{file ? file.name : "Drop an invoice PDF here, or choose one"}</span>
      <span className="text-xs text-slate-500">{file ? `${Math.ceil(file.size / 1024).toLocaleString()} KB · ready to send` : "PDF only. Agent 3 reads it before anyone else."}</span>
      <input type="file" accept="application/pdf,.pdf" className="sr-only" onChange={(e) => pick(e.target.files?.[0])} />
    </label>
  );
}
