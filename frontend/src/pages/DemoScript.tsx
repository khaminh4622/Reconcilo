import { Card, Pill } from "../ui";

type Step = [text: string, scripted?: boolean];

const RUNS: { title: string; steps: Step[] }[] = [
  {
    title: "1. Pay an invoice from a message",
    steps: [
      ["Sign in as Customer A (customer.a@ghostledger.test)."],
      ["Open New case, choose Send a message, and send: “Please pay invoice INV-7781 from Northstar Office Supplies LLC for 1,280.00 USD.” This uses the live AI model. Without one, sign in as the demo admin and run agent4_new_invoice.pdf from Demo scenarios instead.", false],
      ["The case waits for approval. Agent 1 has already checked it, and Customer A has no approve button."],
      ["Sign out and sign in as the bank admin (bank.admin@ghostledger.test)."],
      ["Open Approvals, open the case and press Approve payment."],
      ["Watch the simulated payment, Agent 2's bank reconciliation, and Agent 5's answer. The payment is simulated.", true],
      ["Sign back in as Customer A: the case says the payment is confirmed, and My payments shows the receipt."],
    ],
  },
  {
    title: "2. A payment failure explained by Agent 4",
    steps: [
      ["Sign in as the demo admin (demo.admin@ghostledger.test). Open Demo scenarios and run Agent1_Test_Original_Unpaid_Invoice.pdf for Customer A.", true],
      ["Open the case. In the approve panel, set Developer: simulated bank result to Failed, then approve.", true],
      ["Agent 4 explains the bank's failure code, and Agent 5 tells the customer the invoice was not paid. A retry needs a fresh approval."],
    ],
  },
  {
    title: "3. A case that needs a person",
    steps: [
      ["As the demo admin, run unknown_document_type.pdf (or contradictory_financials.pdf) from Demo scenarios.", true],
      ["The case goes to human review. Agent 5 still answers and says what the customer can do next."],
      ["Or, as Customer A, send a message with no invoice number. The case says exactly what is missing."],
    ],
  },
];

export default function DemoScript() {
  return (
    <div className="mx-auto max-w-[760px] space-y-4">
      <h1 className="text-2xl">Demo script</h1>
      <p className="text-sm">All four demo accounts use the password <code>ghostledger-demo</code>. Everything is simulated, so no real money moves.</p>
      {RUNS.map((run) => (
        <Card key={run.title}>
          <h2 className="mb-2 text-base">{run.title}</h2>
          <ol className="list-decimal space-y-2 pl-5 text-sm">
            {run.steps.map(([text, scripted]) => (
              <li key={text}>
                {text} {scripted === true && <Pill tone="neutral">Scripted</Pill>}
                {scripted === false && <Pill tone="attention">Live AI</Pill>}
              </li>
            ))}
          </ol>
        </Card>
      ))}
      <a className="text-sm text-ocean underline dark:text-sky" href="#/">Back</a>
    </div>
  );
}
