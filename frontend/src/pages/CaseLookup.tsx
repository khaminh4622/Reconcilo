import { useState } from "react";
import { go } from "../router";
import { Button, Card, ErrorText } from "../ui";

/**
 * A bank admin's shortcut to an individual case. The navigation used to send
 * an admin to the approvals list for every `#/cases/…` link, so the case page
 * (and the approve/reject actions on it) was unreachable; this page exists so
 * the route itself has an admin-only destination.
 */
export default function CaseLookup() {
  const [caseId, setCaseId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const open = () => {
    const value = caseId.trim();
    if (!/^[0-9a-fA-F-]{8,64}$/.test(value)) {
      return setError("Paste the full case ID (a UUID), or open a case from the approval queue.");
    }
    setError(null);
    go(`/case/${value}`);
  };

  return (
    <div className="rise mx-auto max-w-2xl space-y-4">
      <h1 className="text-2xl font-semibold">Case lookup</h1>
      <p className="text-sm">
        Open a specific case to authorise or reject its payment. The approval queue lists everything
        that is waiting, so you usually do not need this page.
      </p>
      <Card>
        <label className="block text-sm font-semibold">
          Case ID
          <input
            value={caseId}
            onChange={(e) => setCaseId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && open()}
            placeholder="a5f0e0a1-8f3b-4c1a-9d2e-1b2c3d4e5f60"
            className="mt-1 w-full rounded-xl border border-sky px-3 py-2 font-mono text-sm dark:bg-[#07142f]"
          />
        </label>
        <div className="mt-3 space-y-3">
          <ErrorText error={error} />
          <Button onClick={open}>Open case</Button>
          <p className="text-sm">
            <a href="#/approvals" className="text-ocean underline dark:text-sky">Go to the approval queue</a>
          </p>
        </div>
      </Card>
    </div>
  );
}
