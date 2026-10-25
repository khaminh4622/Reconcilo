import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Svg({ children, ...props }: IconProps & { children: ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden {...props}>
      {children}
    </svg>
  );
}

export const Icons = {
  overview: (p: IconProps) => (
    <Svg {...p}><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" /></Svg>
  ),
  upload: (p: IconProps) => (
    <Svg {...p}><path d="M12 16V5" /><path d="m7.5 9 4.5-4.5L16.5 9" /><path d="M5 19h14" /></Svg>
  ),
  invoices: (p: IconProps) => (
    <Svg {...p}><path d="M7 3.5h7.5L19 8v12.5H7z" /><path d="M14.5 3.5V8H19" /><path d="M9.5 12.5h5" /><path d="M9.5 16h5" /></Svg>
  ),
  payments: (p: IconProps) => (
    <Svg {...p}><rect x="3.5" y="6" width="17" height="12" rx="2" /><path d="M3.5 10h17" /><path d="M7 14.5h4" /></Svg>
  ),
  support: (p: IconProps) => (
    <Svg {...p}><path d="M6 16.5 4 19.5V7.5A2.5 2.5 0 0 1 6.5 5h11A2.5 2.5 0 0 1 20 7.5v6.5a2.5 2.5 0 0 1-2.5 2.5z" /><path d="M8.5 10h7" /><path d="M8.5 13h4" /></Svg>
  ),
  profile: (p: IconProps) => (
    <Svg {...p}><circle cx="12" cy="8.5" r="3" /><path d="M5.5 19c1.2-2.6 3.4-4 6.5-4s5.3 1.4 6.5 4" /></Svg>
  ),
  bank: (p: IconProps) => (
    <Svg {...p}><path d="M4 10h16" /><path d="M12 4 4 10h16z" /><path d="M6 10v7" /><path d="M10 10v7" /><path d="M14 10v7" /><path d="M18 10v7" /><path d="M4 17h16" /><path d="M3 20h18" /></Svg>
  ),
  approvals: (p: IconProps) => (
    <Svg {...p}><path d="M8 12.5 10.5 15 16 9" /><circle cx="12" cy="12" r="8.5" /></Svg>
  ),
  lookup: (p: IconProps) => (
    <Svg {...p}><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></Svg>
  ),
  operations: (p: IconProps) => (
    <Svg {...p}><path d="M4 19V9" /><path d="M10 19V5" /><path d="M16 19v-7" /><path d="M20 19H4" /></Svg>
  ),
  ledger: (p: IconProps) => (
    <Svg {...p}><path d="M5 5.5h11a3 3 0 0 1 3 3V19H8a3 3 0 0 0-3 3z" /><path d="M5 5.5v16" /><path d="M9 9.5h6" /><path d="M9 13h6" /></Svg>
  ),
  alert: (p: IconProps) => (
    <Svg {...p}><path d="M12 4.5 20 19H4z" /><path d="M12 10v4" /><path d="M12 16.5h.01" /></Svg>
  ),
  activity: (p: IconProps) => (
    <Svg {...p}><path d="M4 12h3l2-5 4 10 2-5h5" /></Svg>
  ),
  customers: (p: IconProps) => (
    <Svg {...p}><circle cx="9" cy="9" r="2.5" /><circle cx="16" cy="10" r="2" /><path d="M4.5 18c.7-2.2 2.4-3.3 4.5-3.3s3.8 1.1 4.5 3.3" /><path d="M14 14.8c1.3-.4 2.6-.3 4 .8" /></Svg>
  ),
  scenarios: (p: IconProps) => (
    <Svg {...p}><path d="M8 6h8" /><path d="M7 12h10" /><path d="M9 18h6" /><circle cx="6" cy="6" r="1.2" fill="currentColor" stroke="none" /><circle cx="5" cy="12" r="1.2" fill="currentColor" stroke="none" /><circle cx="7" cy="18" r="1.2" fill="currentColor" stroke="none" /></Svg>
  ),
  script: (p: IconProps) => (
    <Svg {...p}><path d="M7 4.5h8l3 3V19.5H7z" /><path d="M15 4.5V8h3" /><path d="M9.5 12h5" /><path d="M9.5 15.5h3.5" /></Svg>
  ),
  signout: (p: IconProps) => (
    <Svg {...p}><path d="M10 7V5.5A1.5 1.5 0 0 1 11.5 4h7A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-7A1.5 1.5 0 0 1 10 18.5V17" /><path d="M4 12h10" /><path d="m11 8.5 3.5 3.5L11 15.5" /></Svg>
  ),
  menu: (p: IconProps) => (
    <Svg {...p}><path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h16" /></Svg>
  ),
  close: (p: IconProps) => (
    <Svg {...p}><path d="m6 6 12 12" /><path d="M18 6 6 18" /></Svg>
  ),
};

export const NAV_ICONS: Record<string, (p: IconProps) => ReactNode> = {
  dashboard: Icons.overview,
  new: Icons.upload,
  invoices: Icons.invoices,
  payments: Icons.payments,
  investigate: Icons.support,
  profile: Icons.profile,
  bank: Icons.bank,
  approvals: Icons.approvals,
  case: Icons.lookup,
  overview: Icons.operations,
  ledger: Icons.ledger,
  failures: Icons.alert,
  activity: Icons.activity,
  customers: Icons.customers,
  scenarios: Icons.scenarios,
  "demo-script": Icons.script,
};
