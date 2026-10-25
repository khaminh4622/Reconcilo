import { useEffect, useState, type ReactNode } from "react";
import { api, ApiError } from "./api";
import type { Tone } from "./labels";

const TONE: Record<Tone, string> = {
  neutral: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200",
  info: "bg-sky/40 text-navy dark:bg-ocean/30 dark:text-sky",
  attention: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  confirmed: "bg-green-100 text-green-900 dark:bg-green-900/40 dark:text-green-200",
  failure: "bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200",
  check: "bg-teal-100 text-teal-900 dark:bg-teal-900/40 dark:text-teal-200",
};

export function Pill({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-semibold ${TONE[tone]}`}>{children}</span>;
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-2xl bg-white p-5 shadow-sm ring-1 ring-sky/40 dark:bg-[#0e2350] dark:ring-ocean/30 ${className}`}>
      {children}
    </section>
  );
}

/** Details start closed: simple first, depth on request. */
export function Collapse({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="group rounded-2xl bg-white ring-1 ring-sky/40 dark:bg-[#0e2350] dark:ring-ocean/30">
      <summary className="cursor-pointer list-none rounded-2xl px-5 py-4 font-display font-bold">
        <span className="mr-2 inline-block transition-transform group-open:rotate-90" aria-hidden>›</span>
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
    primary: "bg-ocean text-white hover:bg-navy dark:hover:bg-sky dark:hover:text-navy",
    ghost: "bg-transparent text-ocean ring-1 ring-ocean hover:bg-foam dark:text-sky dark:ring-sky dark:hover:bg-ocean/20",
    danger: "bg-red-600 text-white hover:bg-red-700",
  }[kind];
  return (
    <button
      {...props}
      className={`rounded-xl px-4 py-2 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${style} ${className}`}
    />
  );
}

export const ErrorText = ({ error }: { error: string | null }) =>
  error ? <p role="alert" className="rounded-xl bg-red-50 px-4 py-3 text-sm text-red-900 dark:bg-red-900/40 dark:text-red-100">{error}</p> : null;

export const Banner = ({ children }: { children: ReactNode }) => (
  <p className="rounded-xl bg-sky/30 px-4 py-2 text-sm text-navy dark:bg-ocean/25 dark:text-sky">{children}</p>
);

/** The mascot: a small ghost holding a ledger. It floats idle and flips the ledger while working. */
export function Ghost({ working = false, size = 72 }: { working?: boolean; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" role="img" aria-label={working ? "Ghost is working" : "GhostLedger ghost"} className="ghost-float shrink-0">
      <path d="M12 30a20 20 0 0 1 40 0v24l-6-4-7 4-7-4-7 4-6-4-7 4z" fill="#8EC5FF" stroke="#1F6FEB" strokeWidth="2" />
      <circle cx="25" cy="28" r="3" fill="#0B1F4B" />
      <circle cx="39" cy="28" r="3" fill="#0B1F4B" />
      <path d="M28 35q4 3 8 0" stroke="#0B1F4B" strokeWidth="2" fill="none" strokeLinecap="round" />
      <g className={working ? "ledger-flip" : undefined}>
        <rect x="22" y="40" width="20" height="14" rx="2" fill="#fff" stroke="#0B1F4B" strokeWidth="2" />
        <path d="M32 40v14M25 45h4M25 49h4M35 45h4M35 49h4" stroke="#1F6FEB" strokeWidth="1.5" />
      </g>
    </svg>
  );
}

export function Working({ text }: { text: string }) {
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-4 py-6">
      <Ghost working />
      <p className="font-display text-lg font-bold">{text}</p>
    </div>
  );
}

/** Load JSON for a page: { data, error, loading, reload }. */
export function useLoad<T>(path: string | null) {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!path) return;
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
    <div className="fixed inset-0 z-50 flex justify-end bg-navy/40" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="h-full w-full max-w-lg overflow-y-auto bg-white p-5 dark:bg-[#0e2350]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between gap-2">
          <h2 className="text-lg">{title}</h2>
          <Button kind="ghost" onClick={onClose} autoFocus>Close</Button>
        </div>
        {children}
      </aside>
    </div>
  );
}

export const Raw = ({ value }: { value: unknown }) => (
  <pre className="overflow-x-auto rounded-xl bg-foam p-3 text-xs dark:bg-[#07142f]">{JSON.stringify(value, null, 2)}</pre>
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
      <button onClick={show} className="rounded-md bg-foam px-1.5 py-0.5 font-mono text-xs text-ocean underline-offset-2 hover:underline dark:bg-[#07142f] dark:text-sky">
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
  <p className="py-6 text-center text-sm text-slate-500 dark:text-slate-400">{children}</p>
);
