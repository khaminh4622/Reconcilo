import { useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { go } from "../router";
import { money, statusLabel, statusTone, when } from "../labels";
import type { WorkflowCase } from "../types";
import { Button, Card, Drawer, Empty, ErrorText, PageIntro, Pill, Working, useLoad } from "../ui";
import { BankPill, paymentState, type PaymentState } from "./bankStatus";
import { DropPdf } from "./NewCase";

interface InvoiceUpload {
  case_id: string;
  created_at: string;
  updated_at: string;
  status: string;
  source_channel: string | null;
  filename: string | null;
  bank_status: string | null;
}

interface InvoiceRow {
  canonical_id: string;
  case_id: string;
  invoice_number: string | null;
  vendor_name: string | null;
  customer_name: string | null;
  amount: string | null;
  currency: string | null;
  status: string;
  case_status: string;
  bank_status: string | null;
  created_at: string;
  updated_at: string;
  upload_count: number;
  uploads: InvoiceUpload[];
}

const FILTERS: { id: "all" | PaymentState | "none"; label: string }[] = [
  { id: "all", label: "All" },
  { id: "pending", label: "Pending" },
  { id: "approved", label: "Approved" },
  { id: "settled", label: "Settled" },
  { id: "declined", label: "Declined" },
  { id: "failed", label: "Failed" },
  { id: "none", label: "No bank record" },
];

export default function MyInvoices() {
  const { data, error, loading, reload } = useLoad<InvoiceRow[]>("/workflow/invoices");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["id"]>("all");
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const rows = data ?? [];
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((invoice) => {
      if (!matchesFilter(invoice, filter)) return false;
      if (!q) return true;
      const haystack = [invoice.invoice_number, invoice.vendor_name, invoice.customer_name, invoice.case_status, invoice.bank_status]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [rows, query, filter]);
  const open = rows.find((invoice) => invoice.canonical_id === selected) ?? null;

  const upload = async () => {
    if (!file) return setUploadError("Choose a PDF first.");
    setBusy(true);
    setUploadError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const created = await api<WorkflowCase>("/workflow/cases", { method: "POST", body: form });
      go(`/cases/${created.case_id}`);
    } catch (e) {
      setUploadError((e as ApiError).message);
      setBusy(false);
    }
  };

  if (loading && !data) return <Working text="Loading your invoices" />;
  if (busy) return <Working text="Agent 3 is reading your invoice." />;

  return (
    <div className="rise space-y-5">
      <PageIntro
        kicker="Invoices"
        title="Invoice register"
        lede="One row per invoice. Re-uploads stay in that invoice's case history, so they never create a second payment request."
      />
      <ErrorText error={error} />
      <Card>
        <h2 className="text-base font-semibold">Upload a PDF</h2>
        <p className="mt-1 text-sm text-slate-600">Drop the file here. The same intake used by New case sends it to Agent 3.</p>
        <div className="mt-3">
          <DropPdf file={file} onFile={setFile} onError={setUploadError} />
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <Button onClick={upload} disabled={!file || busy}>Send to Reconcilo</Button>
          <button type="button" className="text-sm font-semibold text-ocean hover:underline" onClick={reload}>Refresh list</button>
        </div>
        <div className="mt-3"><ErrorText error={uploadError} /></div>
      </Card>

      <Card className="overflow-hidden p-0">
        <div className="flex flex-col gap-3 border-b border-line px-5 py-4 lg:flex-row lg:items-center lg:justify-between">
          <label className="block min-w-0 flex-1 text-sm font-semibold">
            <span className="sr-only">Search invoices</span>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search invoice, vendor, or status"
              className="field mt-0"
            />
          </label>
          <div role="group" aria-label="Status filters" className="flex flex-wrap gap-1.5">
            {FILTERS.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-pressed={filter === item.id}
                onClick={() => setFilter(item.id)}
                className="rounded-full px-3 py-1 text-xs font-semibold ring-1 ring-line aria-pressed:bg-navy aria-pressed:text-white aria-pressed:ring-navy"
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
        {rows.length === 0 ? (
          <div className="p-5"><Empty>No invoices yet. Drop a PDF above, or <a href="#/new" className="font-semibold text-ocean hover:underline">open New case</a>.</Empty></div>
        ) : visible.length === 0 ? (
          <div className="p-5"><Empty>No invoices match that search.</Empty></div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data">
              <thead>
                <tr>
                  {["Invoice", "Vendor", "Amount", "Status", "Updated", ""].map((heading) => <th key={heading || "action"}>{heading}</th>)}
                </tr>
              </thead>
              <tbody>
                {visible.map((invoice) => (
                  <tr key={invoice.canonical_id} aria-selected={selected === invoice.canonical_id}>
                    <td className="font-semibold">{invoice.invoice_number ?? "No invoice number"}</td>
                    <td>{invoice.vendor_name ?? "Unknown vendor"}</td>
                    <td className="whitespace-nowrap font-medium">{money(invoice.amount, invoice.currency)}</td>
                    <td>{invoice.bank_status
                      ? <BankPill status={invoice.bank_status} />
                      : <Pill tone={statusTone(invoice.case_status as never)}>{statusLabel(invoice.case_status as never)}</Pill>}
                    </td>
                    <td className="whitespace-nowrap text-slate-500">{when(invoice.updated_at)}</td>
                    <td className="text-right">
                      <button type="button" className="text-sm font-semibold text-ocean hover:underline" onClick={() => setSelected(invoice.canonical_id)}>
                        Details
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {open && (
        <Drawer title={open.invoice_number ?? "Invoice"} onClose={() => setSelected(null)}>
          <InvoiceDetail invoice={open} />
        </Drawer>
      )}
    </div>
  );
}

function matchesFilter(invoice: InvoiceRow, filter: (typeof FILTERS)[number]["id"]) {
  if (filter === "all") return true;
  if (filter === "none") return !invoice.bank_status;
  if (!invoice.bank_status) return false;
  return paymentState(invoice.bank_status) === filter;
}

function InvoiceDetail({ invoice }: { invoice: InvoiceRow }) {
  return (
    <div className="space-y-4">
      <Card>
        <dl className="grid grid-cols-2 gap-3 text-sm">
          <Fact label="Vendor" value={invoice.vendor_name ?? "—"} />
          <Fact label="Amount" value={money(invoice.amount, invoice.currency)} />
          <Fact label="Customer on document" value={invoice.customer_name ?? "—"} />
          <Fact label="Uploads" value={String(invoice.upload_count)} />
          <Fact label="First uploaded" value={when(invoice.created_at)} />
          <Fact label="Updated" value={when(invoice.updated_at)} />
        </dl>
        <div className="mt-3">
          {invoice.bank_status
            ? <BankPill status={invoice.bank_status} />
            : <Pill tone={statusTone(invoice.case_status as never)}>{statusLabel(invoice.case_status as never)}</Pill>}
        </div>
        <a href={`#/cases/${invoice.case_id}`} className="mt-4 inline-block text-sm font-semibold text-ocean hover:underline">
          Open the AI workflow
        </a>
      </Card>
      <Card>
        <h3 className="text-sm font-semibold">Case history</h3>
        <ol className="mt-3 space-y-2">
          {invoice.uploads.map((upload) => (
            <li key={upload.case_id} className="rounded-xl bg-foam px-3 py-2 text-sm">
              <a href={`#/cases/${upload.case_id}`} className="font-semibold text-ocean hover:underline">
                {upload.filename ?? "Invoice record"}
              </a>
              <p className="text-xs text-slate-500">{when(upload.created_at)} · {upload.source_channel ?? "record"}</p>
              <div className="mt-1">
                {upload.bank_status
                  ? <BankPill status={upload.bank_status} />
                  : <Pill tone={statusTone(upload.status as never)}>{statusLabel(upload.status as never)}</Pill>}
              </div>
            </li>
          ))}
        </ol>
      </Card>
      <p className="text-xs leading-5 text-slate-500">
        Settled means the simulated bank committed both sides of the transfer and Agent 2 matched it. Pending means a bank administrator still has to authorise it.
      </p>
    </div>
  );
}

const Fact = ({ label, value }: { label: string; value: string }) => (
  <div>
    <dt className="text-xs text-slate-500">{label}</dt>
    <dd className="font-semibold">{value}</dd>
  </div>
);
