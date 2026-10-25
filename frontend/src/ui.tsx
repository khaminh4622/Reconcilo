import { useEffect, useState, type ReactNode } from "react";
import { api, ApiError } from "./api";
import type { Tone } from "./labels";

const TONE: Record<Tone, string> = {
  neutral: "bg-slate-100 text-slate-700",
  info: "bg-sky/50 text-tide",
  attention: "bg-amber-100 text-amber-950",
  confirmed: "bg-emerald-100 text-emerald-950",
  failure: "bg-red-100 text-red-900",
  check: "bg-teal-100 text-teal-950",
};

export function Pill({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${TONE[tone]}`}>{children}</span>;
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-2xl bg-white p-5 shadow-[0_1px_2px_rgba(7,24,51,0.04),0_12px_32px_-18px_rgba(7,24,51,0.28)] ring-1 ring-line/80 ${className}`}>
      {children}
    </section>
  );
}

export function PageIntro({
  kicker, title, lede, actions,
}: {
  kicker?: string;
  title: string;
  lede?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="max-w-2xl">
        {kicker && <p className="text-xs font-bold uppercase tracking-[0.16em] text-ocean">{kicker}</p>}
        <h1 className="mt-1 text-[1.75rem] font-semibold leading-tight">{title}</h1>
        {lede && <p className="mt-1.5 text-sm leading-6 text-slate-600">{lede}</p>}
      </div>
      {actions}
    </div>
  );
}

export function Metric({
  label, value, hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card className="min-h-[132px]">
      <p className="text-[0.7rem] font-bold uppercase tracking-[0.14em] text-slate-500">{label}</p>
      <p className="mt-3 font-display text-[1.85rem] font-medium leading-none tracking-tight text-navy">{value}</p>
      {hint && <p className="mt-3 text-sm leading-5 text-slate-600">{hint}</p>}
    </Card>
  );
}

/** Details start closed: simple first, depth on request. */
export function Collapse({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="group rounded-2xl bg-white shadow-sm ring-1 ring-line/80">
      <summary className="cursor-pointer list-none rounded-2xl px-5 py-4 text-sm font-semibold">
        <span className="mr-2 inline-block text-ocean transition-transform group-open:rotate-90" aria-hidden>›</span>
        {title}
      </summary>
      <div className="px-5 pb-5">{children}</div>
    </details>
  );
}

export function Button({
  kind = "primary", className = "", ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { kind?: "primary" | "ghost" | "danger" }) {
  const style = {
    primary: "bg-ocean text-white shadow-sm hover:bg-navy",
    ghost: "bg-white text-ink ring-1 ring-line hover:bg-foam hover:ring-ocean/40",
    danger: "bg-red-700 text-white hover:bg-red-800",
  }[kind];
  return (
    <button
      {...props}
      className={`rounded-xl px-4 py-2 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${style} ${className}`}
    />
  );
}

export const ErrorText = ({ error }: { error: string | null }) =>
  error ? (
    <p role="alert" className="rounded-xl bg-red-50 px-4 py-3 text-sm text-red-900 ring-1 ring-red-200">{error}</p>
  ) : null;

export const Banner = ({ children }: { children: ReactNode }) => (
  <p className="rounded-xl bg-white px-4 py-2.5 text-sm text-tide shadow-sm ring-1 ring-line">{children}</p>
);

/** The mascot: a small ghost holding a ledger. It floats idle and flips the ledger while working. */
export function Ghost({ working = false, size = 72 }: { working?: boolean; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" role="img" aria-label={working ? "Ghost is working" : "GhostLedger ghost"} className="ghost-float shrink-0">
      <path d="M12 30a20 20 0 0 1 40 0v24l-6-4-7 4-7-4-7 4-6-4-7 4z" fill="#D7EFEA" stroke="#0F6E6B" strokeWidth="2" />
      <circle cx="25" cy="28" r="2.4" fill="#071833" />
      <circle cx="39" cy="28" r="2.4" fill="#071833" />
      <path d="M28 35q4 3 8 0" stroke="#071833" strokeWidth="1.8" fill="none" strokeLinecap="round" />
      <g className={working ? "ledger-flip" : undefined}>
        <rect x="22" y="40" width="20" height="14" rx="2" fill="#fff" stroke="#071833" strokeWidth="1.6" />
        <path d="M32 40v14M25 45h4M25 49h4M35 45h4M35 49h4" stroke="#0F6E6B" strokeWidth="1.4" />
      </g>
    </svg>
  );
}

export function Working({ text }: { text: string }) {
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-4 py-8">
      <Ghost working size={64} />
      <div>
        <p className="text-xs font-bold uppercase tracking-[0.14em] text-ocean">Working</p>
        <p className="text-lg font-semibold">{text}</p>
      </div>
    </div>
  );
}

/** Load JSON for a page: { data, error, loading, reload }. */
export function useLoad<T>(path: string | null) {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!path) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    api<T>(path)
      .then((data) => live && setState({ data, error: null, loading: false }))
      .catch((e: ApiError) => live && setState({ data: null, error: e.message, loading: false }));
    return () => {
      live = false;
    };
  }, [path, tick]);
  return { ...state, reload: () => setTick((t) => t + 1) };
}

/** Raw record viewer: a side drawer, opened from a record chip. */
export function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-navy/50" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="h-full w-full max-w-lg overflow-y-auto bg-foam p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">{title}</h2>
          <Button kind="ghost" onClick={onClose} autoFocus>Close</Button>
        </div>
        {children}
      </aside>
    </div>
  );
}

export const Raw = ({ value }: { value: unknown }) => (
  <pre className="overflow-x-auto rounded-xl bg-navy p-3 text-xs leading-5 text-sky">{JSON.stringify(value, null, 2)}</pre>
);

/** A clickable ID chip. Opens `load()`'s record (or `value`) in a drawer. */
export function RecordChip({ label, value, load }: { label: string; value?: unknown; load?: () => Promise<unknown> }) {
  const [open, setOpen] = useState(false);
  const [record, setRecord] = useState<unknown>(value);
  const [error, setError] = useState<string | null>(null);
  const show = () => {
    setOpen(true);
    if (load) load().then(setRecord).catch((e: ApiError) => setError(e.message));
  };
  return (
    <>
      <button onClick={show} className="rounded-md bg-foam px-1.5 py-0.5 font-mono text-xs text-ocean underline-offset-2 ring-1 ring-line hover:underline">
        {label}
      </button>
      {open && (
        <Drawer title={label} onClose={() => setOpen(false)}>
          <ErrorText error={error} />
          {record !== undefined && <Raw value={record} />}
        </Drawer>
      )}
    </>
  );
}

export const Empty = ({ children }: { children: ReactNode }) => (
  <div className="rounded-2xl border border-dashed border-line bg-white/70 px-4 py-8 text-center text-sm text-slate-500">{children}</div>
);
