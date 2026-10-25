import { useSession } from "../App";
import { when } from "../labels";
import { Card } from "../ui";

export default function Profile() {
  const { me } = useSession();
  if (!me) return null;
  const { account } = me;
  return (
    <div className="mx-auto max-w-[760px] space-y-3">
      <h1 className="text-2xl">Profile</h1>
      <Card>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
          <dt className="font-semibold">Name</dt><dd>{account.display_name}</dd>
          <dt className="font-semibold">Email</dt><dd>{account.email}</dd>
          <dt className="font-semibold">Role</dt><dd>Customer</dd>
          <dt className="font-semibold">Member since</dt><dd>{when(account.created_at)}</dd>
        </dl>
      </Card>
    </div>
  );
}
