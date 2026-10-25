import { useState, type FormEvent } from "react";
import { api, ApiError, setToken } from "../api";
import { go } from "../router";
import { Logo } from "../logo";
import { Button, Card, ErrorText, Pill } from "../ui";

// Seeded by `python -m tools.seed_demo_accounts` (backend/README.md).
export const DEMO_ACCOUNTS = [
  ["Customer A", "customer.a@ghostledger.test"],
  ["Customer B", "customer.b@ghostledger.test"],
  ["Bank admin", "bank.admin@ghostledger.test"],
  ["Demo admin", "demo.admin@ghostledger.test"],
];
export const DEMO_PASSWORD = "ghostledger-demo";
const isLocal = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);

export default function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = mode === "signup"
        ? await api<{ token: string }>("/auth/signup", { method: "POST", json: { email, password, display_name: name } })
        : await api<{ token: string }>("/auth/signin", { method: "POST", json: { email, password } });
      setToken(result.token);
      go("/");
      onSignedIn();
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid min-h-screen lg:grid-cols-[1.05fr_0.95fr]">
      <section className="relative hidden overflow-hidden bg-navy px-12 py-14 text-white lg:flex lg:flex-col lg:justify-between">
        <div>
          <div>
            <Logo tone="onDark" mark={52} float />
            <p className="mt-2 text-sm text-sky">Simulated treasury · human approval required</p>
          </div>
          <h1 className="mt-16 max-w-md font-display text-5xl font-medium leading-[1.05] text-white">
            Invoices, agents, and a bank that waits for a person.
          </h1>
          <p className="mt-5 max-w-md text-base leading-7 text-sky">
            Agent 3 reads the document. Agent 1 checks for duplicates. No payment moves until a bank administrator approves the exact amount.
          </p>
        </div>
        <ol className="grid max-w-lg grid-cols-3 gap-4 text-sm">
          {[
            ["01", "Verify", "Figures checked before routing."],
            ["02", "Approve", "Customers cannot approve their own pay."],
            ["03", "Settle", "Simulated funds, real audit trail."],
          ].map(([n, title, body]) => (
            <li key={n}>
              <p className="font-display text-2xl text-teal-200">{n}</p>
              <p className="mt-1 font-semibold">{title}</p>
              <p className="mt-1 text-xs leading-5 text-sky">{body}</p>
            </li>
          ))}
        </ol>
      </section>
      <section className="flex items-center justify-center px-4 py-10">
        <div className="w-full max-w-md space-y-4">
          <div className="lg:hidden">
            <Logo tone="onLight" mark={44} />
            <p className="mt-2 text-sm text-slate-600">Simulated treasury</p>
          </div>
          <div>
            <h2 className="text-2xl font-semibold">{mode === "signup" ? "Create a customer account" : "Sign in"}</h2>
            <p className="mt-1 text-sm text-slate-600">
              {mode === "signup"
                ? "New accounts are customers. Bank administrator access is issued separately."
                : "Pay invoices and ask about payments. Nothing settles without a bank decision."}
            </p>
          </div>
          <Card>
            <form onSubmit={submit} className="space-y-3">
              {mode === "signup" && (
                <label className="block text-sm font-semibold">
                  Name
                  <input className="field" required value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
                </label>
              )}
              <label className="block text-sm font-semibold">
                Email
                <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" className="field" />
              </label>
              <label className="block text-sm font-semibold">
                Password
                <input type="password" required minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} autoComplete={mode === "signup" ? "new-password" : "current-password"} className="field" />
              </label>
              <ErrorText error={error} />
              <Button type="submit" disabled={busy} className="w-full">
                {busy ? (mode === "signup" ? "Creating account…" : "Signing in…") : (mode === "signup" ? "Create account" : "Sign in")}
              </Button>
            </form>
          </Card>
          <p className="text-center text-sm text-slate-600">
            {mode === "signup" ? "Already have an account?" : "New here?"}{" "}
            <button
              type="button"
              className="font-semibold text-ocean underline-offset-2 hover:underline"
              onClick={() => { setMode(mode === "signup" ? "signin" : "signup"); setError(null); }}
            >
              {mode === "signup" ? "Sign in" : "Create account"}
            </button>
          </p>
          {isLocal && mode === "signin" && (
            <Card>
              <div className="mb-2 flex items-center gap-2">
                <h3 className="text-base font-semibold">Demo accounts</h3>
                <Pill tone="attention">Local demo</Pill>
              </div>
              <p className="mb-3 text-sm text-slate-600">Fills in a seeded login. Password: <code className="rounded bg-foam px-1.5 py-0.5 font-mono text-xs">{DEMO_PASSWORD}</code></p>
              <div className="grid grid-cols-2 gap-2">
                {DEMO_ACCOUNTS.map(([label, demoEmail]) => (
                  <Button key={demoEmail} kind="ghost" onClick={() => { setEmail(demoEmail); setPassword(DEMO_PASSWORD); }}>
                    {label}
                  </Button>
                ))}
              </div>
            </Card>
          )}
          <p className="text-center text-sm"><a className="font-semibold text-ocean hover:underline" href="#/demo-script">Demo script</a></p>
        </div>
      </section>
    </div>
  );
}
