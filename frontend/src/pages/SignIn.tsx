import { useState } from "react";
import { api, ApiError, setToken } from "../api";
import { go } from "../router";
import { Button, Card, ErrorText, Ghost, Pill } from "../ui";

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
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ token: string }>("/auth/signin", { method: "POST", json: { email, password } });
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
    <div className="mx-auto max-w-md space-y-4">
      <div className="flex items-center gap-3">
        <Ghost />
        <div>
          <h1 className="text-2xl">Sign in</h1>
          <p className="text-sm text-slate-600 dark:text-slate-300">Pay invoices and get answers about your payments.</p>
        </div>
      </div>
      <Card>
        <form onSubmit={submit} className="space-y-3">
          <label className="block text-sm font-semibold">
            Email
            <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username"
              className="mt-1 w-full rounded-xl border border-sky px-3 py-2 dark:bg-[#07142f]" />
          </label>
          <label className="block text-sm font-semibold">
            Password
            <input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password"
              className="mt-1 w-full rounded-xl border border-sky px-3 py-2 dark:bg-[#07142f]" />
          </label>
          <ErrorText error={error} />
          <Button type="submit" disabled={busy} className="w-full">{busy ? "Signing in…" : "Sign in"}</Button>
        </form>
      </Card>
      {isLocal && (
        <Card>
          <div className="mb-2 flex items-center gap-2">
            <h2 className="text-base">Demo accounts</h2>
            <Pill tone="attention">Local demo</Pill>
          </div>
          <p className="mb-3 text-sm">Fills in a seeded login. Password: <code>{DEMO_PASSWORD}</code></p>
          <div className="grid grid-cols-2 gap-2">
            {DEMO_ACCOUNTS.map(([label, demoEmail]) => (
              <Button key={demoEmail} kind="ghost" onClick={() => { setEmail(demoEmail); setPassword(DEMO_PASSWORD); }}>
                {label}
              </Button>
            ))}
          </div>
        </Card>
      )}
      <p className="text-center text-sm"><a className="text-ocean underline dark:text-sky" href="#/demo-script">Demo script</a></p>
    </div>
  );
}
