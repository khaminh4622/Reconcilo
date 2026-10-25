import { money, statusLabel, statusTone, when } from "../labels";
import { Card, Collapse, Empty, ErrorText, Pill, Working, useLoad } from "../ui";
import { BankPill } from "./CustomerDashboard";

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

export default function MyInvoices() {
  const { data, error, loading } = useLoad<InvoiceRow[]>("/workflow/invoices");
  if (loading) return <Working text="Loading your invoices" />;
  return (
    <div className="space-y-3">
      <h1 className="text-2xl">My invoices</h1>
      <p className="text-sm">
        One row per invoice. Re-uploads stay in that invoice&apos;s case history, so they never create a
        second payment request or obscure the original payment status.
      </p>
      <ErrorText error={error} />
      {data?.length === 0 && <Empty>No invoices yet. <a href="#/new" className="text-ocean underline dark:text-sky">Upload one</a>.</Empty>}
      {data?.map((invoice) => (
        <Card key={invoice.canonical_id}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <a href={`#/cases/${invoice.case_id}`} className="font-semibold text-ocean underline dark:text-sky">
              {invoice.invoice_number ?? "No invoice number"} · {invoice.vendor_name ?? "Unknown vendor"}
            </a>
            <span className="flex flex-wrap items-center gap-2">
              <span className="font-semibold">{money(invoice.amount, invoice.currency)}</span>
              {invoice.bank_status
                ? <BankPill status={invoice.bank_status} />
                : <Pill tone={statusTone(invoice.case_status as any)}>{statusLabel(invoice.case_status as any)}</Pill>}
            </span>
          </div>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
            First uploaded {when(invoice.created_at)} · Updated {when(invoice.updated_at)}
          </p>
          <Collapse title={`${invoice.upload_count} ${invoice.upload_count === 1 ? "upload" : "uploads"} in case history`}>
            <ol className="space-y-2 text-sm">
              {invoice.uploads.map((upload) => (
                <li key={upload.case_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-foam px-3 py-2 dark:bg-[#07142f]">
                  <span>
                    <a href={`#/cases/${upload.case_id}`} className="text-ocean underline dark:text-sky">
                      {upload.filename ?? "Invoice record"}
                    </a>
                    <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">{when(upload.created_at)}</span>
                  </span>
                  {upload.bank_status
                    ? <BankPill status={upload.bank_status} />
                    : <Pill tone={statusTone(upload.status as any)}>{statusLabel(upload.status as any)}</Pill>}
                </li>
              ))}
            </ol>
          </Collapse>
        </Card>
      ))}
      <Card>
        <h2 className="text-base">A note on payment status</h2>
        <p className="mt-1 text-sm">
          “Settled” means the simulated bank committed both sides of the transfer and Agent 2 matched
          it. <BankPill status="PENDING_APPROVAL" /> means a bank administrator still has to authorise it.
        </p>
      </Card>
    </div>
  );
}
