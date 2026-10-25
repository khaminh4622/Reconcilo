import { useState } from "react";
import { api, ApiError } from "../api";
import { go } from "../router";
import type { WorkflowCase } from "../types";
import { Button, Card, ErrorText, Working } from "../ui";

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

  const pick = (chosen: File | undefined) => {
    if (!chosen) return;
    if (!chosen.name.toLowerCase().endsWith(".pdf")) return setError("Only PDF files can be uploaded.");
    setError(null);
    setFile(chosen);
  };

  if (busy) return <Working text="Agent 3 is reading your request. This can take a little while." />;

  return (
    <div className="mx-auto max-w-[760px] space-y-4">
      <h1 className="text-2xl">New case</h1>
      <div role="tablist" className="flex gap-2">
        {([["pdf", "Upload a PDF"], ["message", "Send a message"]] as const).map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
            className="rounded-xl px-4 py-2 text-sm font-semibold ring-1 ring-ocean aria-selected:bg-ocean aria-selected:text-white">
            {label}
          </button>
        ))}
      </div>
      <Card>
        {tab === "pdf" ? (
          <label
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files[0]); }}
            className="flex cursor-pointer flex-col items-center gap-2 rounded-xl border-2 border-dashed border-sky p-8 text-center"
          >
            <span className="font-semibold">{file ? file.name : "Drop an invoice PDF here, or choose one"}</span>
            <span className="text-xs text-slate-500 dark:text-slate-400">PDF only</span>
            <input type="file" accept="application/pdf,.pdf" className="sr-only" onChange={(e) => pick(e.target.files?.[0])} />
          </label>
        ) : (
          <label className="block text-sm font-semibold">
            Your message
            <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={6} maxLength={MAX_MESSAGE}
              placeholder="For example: Please pay invoice INV-7781 from Northstar Office Supplies LLC for 1,280.00 USD."
              className="mt-1 w-full rounded-xl border border-sky px-3 py-2 font-normal dark:bg-[#07142f]" />
            <span className="text-xs font-normal text-slate-500 dark:text-slate-400">
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
