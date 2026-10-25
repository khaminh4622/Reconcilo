import { when } from "../labels";
import { Card, Empty, ErrorText, Working, useLoad } from "../ui";

export interface CustomerRow {
  account_id: string;
  display_name: string;
  email: string;
  case_count: number;
  last_activity: string | null;
}

export default function Customers() {
  const { data, error, loading } = useLoad<{ customers: CustomerRow[] }>("/admin/customers");
  if (loading) return <Working text="Loading customers" />;
  return (
    <div className="space-y-3">
      <h1 className="text-2xl">Customers</h1>
      <ErrorText error={error} />
      {data?.customers.length === 0 && <Empty>No customer accounts yet.</Empty>}
      <Card className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr><th className="py-2">Name</th><th>Email</th><th>Cases</th><th>Last activity</th></tr></thead>
          <tbody>
            {data?.customers.map((c) => (
              <tr key={c.account_id} className="border-t border-sky/40">
                <td className="py-2 font-semibold">{c.display_name}</td><td>{c.email}</td><td>{c.case_count}</td><td>{when(c.last_activity)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
