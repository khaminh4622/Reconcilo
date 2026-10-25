import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, getToken, setToken, setUnauthorizedHandler } from "./api";
import { NAV_ICONS } from "./icons";
import { go, useRoute } from "./router";
import type { Account, Health } from "./types";
import { Banner, Ghost, Working } from "./ui";
import SignIn from "./pages/SignIn";
import NewCase from "./pages/NewCase";
import MyCases from "./pages/MyCases";
import CasePage from "./pages/CasePage";
import MyPayments from "./pages/MyPayments";
import Profile from "./pages/Profile";
import Approvals from "./pages/Approvals";
import Overview from "./pages/Overview";
import Failures from "./pages/Failures";
import Activity from "./pages/Activity";
import Customers from "./pages/Customers";
import Ledger from "./pages/Ledger";
import Scenarios from "./pages/Scenarios";
import DemoScript from "./pages/DemoScript";
import CustomerDashboard from "./pages/CustomerDashboard";
import MyInvoices from "./pages/MyInvoices";
import Investigate from "./pages/Investigate";
import BankAdmin from "./pages/BankAdmin";
import CaseLookup from "./pages/CaseLookup";

export interface Me {
  account: Account;
  payment_history: Record<string, any>[];
  receipts: Record<string, any>[];
  cases?: Record<string, any>[];
  bank_payments?: Record<string, any>[];
  bank_account?: Record<string, any> | null;
}

export const SessionContext = createContext<{ me: Me | null; health: Health | null }>({ me: null, health: null });
export const useSession = () => useContext(SessionContext);

const CUSTOMER_NAV = [
  ["dashboard", "Overview"], ["new", "New case"], ["invoices", "Invoices"],
  ["payments", "Payments"], ["investigate", "AI support"], ["profile", "Profile"],
];
const ADMIN_NAV = [
  ["bank", "Bank desk"], ["approvals", "Approvals"], ["case", "Case lookup"],
  ["overview", "Operations"], ["ledger", "Reconciliation"], ["failures", "Failures"],
  ["activity", "Activity"], ["customers", "Customers"],
];
const DEMO_NAV = [["scenarios", "Demo scenarios"], ["demo-script", "Demo script"]];
const ADMIN_PAGES = new Set([
  "bank", "approvals", "case", "overview", "ledger", "failures", "activity", "customers", "scenarios",
]);
const CUSTOMER_PAGES = new Set(["dashboard", "new", "invoices", "payments", "investigate", "profile"]);

const ROLE_LABEL: Record<Account["role"], string> = {
  CUSTOMER: "Customer",
  BANK_ADMIN: "Bank administrator",
  DEMO_ADMIN: "Demo administrator",
};

export function navFor(role: Account["role"]): string[][] {
  if (role === "CUSTOMER") return CUSTOMER_NAV;
  if (role === "DEMO_ADMIN") return DEMO_NAV;
  return ADMIN_NAV;
}

export default function App() {
  const route = useRoute();
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(Boolean(getToken()));
  const [health, setHealth] = useState<Health | null>(null);

  const loadMe = useCallback(async () => {
    if (!getToken()) return setMe(null);
    setLoading(true);
    try {
      setMe(await api<Me>("/auth/me"));
    } catch {
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setMe(null);
      go("/signin");
    });
    loadMe();
    api<Health>("/health").then(setHealth).catch(() => setHealth(null));
  }, [loadMe]);

  const signOut = async () => {
    await api("/auth/signout", { method: "POST" }).catch(() => {});
    setToken(null);
    setMe(null);
    go("/signin");
  };

  const page = route[0] ?? "";
  let content;
  if (page === "demo-script") content = <DemoScript />;
  else if (loading) content = <Working text="Opening GhostLedger" />;
  else if (!me) content = <SignIn onSignedIn={loadMe} />;
  else content = <Page me={me} route={route} />;

  return (
    <SessionContext.Provider value={{ me, health }}>
      <AppFrame me={me} health={health} onSignOut={signOut} current={page} bare={!me && page !== "demo-script"}>
        {health && !health.llm_configured && (
          <Banner>Live AI analysis is unavailable on this server. Scripted demo scenarios still work.</Banner>
        )}
        {content}
      </AppFrame>
    </SessionContext.Provider>
  );
}

export function Page({ me, route }: { me: Me; route: string[] }) {
  const role = me.account.role;
  const isBankAdmin = role === "BANK_ADMIN";
  const page = route[0] ?? (isBankAdmin ? "bank" : role === "DEMO_ADMIN" ? "scenarios" : "dashboard");

  if (!isBankAdmin && ADMIN_PAGES.has(page)) return <Notice text="This area is for bank admins." />;
  if (role !== "CUSTOMER" && CUSTOMER_PAGES.has(page)) return <Notice text="This area is for customers." />;
  if (page === "scenarios" && role !== "DEMO_ADMIN") return <Notice text="Demo scenarios are for the demo admin." />;

  switch (page) {
    case "dashboard": return <CustomerDashboard />;
    case "new": return <NewCase />;
    case "invoices": return <MyInvoices />;
    case "investigate": return <Investigate />;
    case "payments": return <MyPayments />;
    case "profile": return <Profile />;
    // A bank admin opens an individual case here; customers reach it by link.
    case "cases": return route[1] ? <CasePage caseId={route[1]} /> : isBankAdmin ? <Approvals /> : <MyCases />;
    case "case": return route[1] ? <CasePage caseId={route[1]} /> : <CaseLookup />;
    case "approvals": return <Approvals />;
    case "bank": return <BankAdmin />;
    case "overview": return <Overview />;
    case "failures": return <Failures />;
    case "activity": return <Activity />;
    case "customers": return <Customers />;
    case "ledger": return <Ledger />;
    case "scenarios": return <Scenarios />;
    default: return <Notice text="That page doesn't exist." />;
  }
}

const Notice = ({ text }: { text: string }) => (
  <div className="flex flex-col items-center gap-3 py-16 text-center">
    <Ghost />
    <p className="text-xl font-semibold">{text}</p>
  </div>
);

function AppFrame({
  me, health, onSignOut, current, bare, children,
}: {
  me: Me | null;
  health: Health | null;
  onSignOut: () => void;
  current: string;
  bare: boolean;
  children: ReactNode;
}) {
  const [menu, setMenu] = useState(false);
  if (bare || !me) {
    return <div className="min-h-screen bg-foam">{children}</div>;
  }
  return (
    <div className="min-h-screen bg-foam lg:grid lg:grid-cols-[272px_minmax(0,1fr)]">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      {menu && (
        <button aria-label="Close menu" className="fixed inset-0 z-30 bg-navy/50 lg:hidden" onClick={() => setMenu(false)} />
      )}
      <Sidebar me={me} health={health} current={current} onSignOut={onSignOut} open={menu} onNavigate={() => setMenu(false)} />
      <div className="min-w-0">
        <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-line/80 bg-foam/90 px-4 py-3 backdrop-blur lg:hidden">
          <button
            type="button"
            className="rounded-xl bg-navy p-2 text-white"
            aria-expanded={menu}
            aria-label={menu ? "Close navigation" : "Open navigation"}
            onClick={() => setMenu((open) => !open)}
          >
            <span className="block h-5 w-5">{menu ? "✕" : "☰"}</span>
          </button>
          <span className="font-semibold">GhostLedger</span>
          <HealthDot health={health} className="ml-auto" onLight />
        </header>
        <main id="main" className="mx-auto max-w-6xl space-y-5 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
          {children}
        </main>
      </div>
    </div>
  );
}

function Sidebar({
  me, health, current, onSignOut, open, onNavigate,
}: {
  me: Me;
  health: Health | null;
  current: string;
  onSignOut: () => void;
  open: boolean;
  onNavigate: () => void;
}) {
  const nav = navFor(me.account.role);
  return (
    <aside className={`fixed inset-y-0 left-0 z-40 flex w-[272px] flex-col bg-navy text-white transition-transform lg:static lg:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}>
      <div className="flex items-center gap-3 px-5 pb-4 pt-6">
        <Ghost size={42} />
        <div>
          <a href="#/" className="block text-lg font-semibold tracking-tight" onClick={onNavigate}>GhostLedger</a>
          <p className="text-xs text-sky/90">Simulated treasury</p>
        </div>
      </div>
      <div className="px-5 pb-4">
        <HealthDot health={health} />
      </div>
      <nav aria-label="Main" className="flex-1 space-y-1 overflow-y-auto px-3">
        {nav.map(([path, label]) => {
          const Icon = NAV_ICONS[path];
          const active = current === path || (path === "dashboard" && current === "");
          return (
            <a
              key={path}
              href={`#/${path}`}
              aria-current={active ? "page" : undefined}
              onClick={onNavigate}
              className="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium text-sky hover:bg-white/8 hover:text-white aria-[current=page]:bg-white/12 aria-[current=page]:text-white"
            >
              {Icon && <Icon className="h-[18px] w-[18px] shrink-0 opacity-90" />}
              {label}
            </a>
          );
        })}
      </nav>
      <div className="border-t border-white/10 p-4">
        <p className="truncate text-sm font-semibold">{me.account.display_name}</p>
        <p className="truncate text-xs text-sky/80">{me.account.email}</p>
        <p className="mt-1 text-[0.7rem] font-bold uppercase tracking-[0.14em] text-teal-200/90">{ROLE_LABEL[me.account.role]}</p>
        <button onClick={onSignOut} className="mt-3 w-full rounded-xl px-3 py-2 text-left text-sm text-sky hover:bg-white/10 hover:text-white">
          Sign out
        </button>
      </div>
    </aside>
  );
}

function HealthDot({ health, className = "", onLight = false }: { health: Health | null; className?: string; onLight?: boolean }) {
  const dot = health === null ? "bg-slate-400" : health.llm_configured ? "bg-emerald-500" : "bg-amber-500";
  const dotText = health === null ? "Server not reachable" : health.llm_configured ? `Live AI ready (${health.llm_model})` : "Live AI not configured";
  return (
    <span className={`flex items-center gap-2 text-xs ${onLight ? "text-slate-600" : "text-sky"} ${className}`} title={dotText}>
      <span className={`h-2 w-2 rounded-full ${dot}`} aria-hidden />
      <span className="max-w-[14rem] truncate">{dotText}</span>
    </span>
  );
}
