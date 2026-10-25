/**
 * Reconcilo mark: a coin with an inner rim, a geometric R, and rising ledger bars.
 * `onDark` sits on the navy sidebar. `onLight` sits on the pale canvas.
 */
export function LogoMark({
  size = 32,
  tone = "onDark",
  working = false,
  title = "Reconcilo",
}: {
  size?: number;
  tone?: "onDark" | "onLight";
  working?: boolean;
  title?: string;
}) {
  const coin = tone === "onDark" ? "#128F88" : "#071833";
  const rim = tone === "onDark" ? "#D7F4F1" : "#7ED9D2";
  const ink = "#F7FBFA";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label={title}
      className={working ? "logo-pulse shrink-0" : "shrink-0"}
    >
      <circle cx="32" cy="32" r="29.2" fill={coin} />
      <circle cx="32" cy="32" r="29.2" fill="none" stroke={rim} strokeWidth="1.6" opacity="0.9" />
      <circle cx="32" cy="32" r="24.2" fill="none" stroke={rim} strokeWidth="1.15" opacity="0.72" />
      <g fill={ink}>
        <rect x="14.8" y="16.4" width="6.2" height="31.2" rx="1.2" />
        <path d="M26.2 31.6 36.6 47.2h-4.6L22.6 32.8z" />
        <rect x="39.2" y="36.4" width="3.5" height="11.2" rx="0.8" />
        <rect x="43.8" y="30.2" width="3.5" height="17.4" rx="0.8" />
        <rect x="48.4" y="23.2" width="3.5" height="24.4" rx="0.8" />
      </g>
      <path
        d="M21.2 19.4h6.4c5.4 0 8.6 2.6 8.6 6.7s-3.2 6.7-8.6 6.7h-6.4"
        fill="none"
        stroke={ink}
        strokeWidth="6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Logo({
  tone = "onDark",
  mark = 36,
  className = "",
}: {
  tone?: "onDark" | "onLight";
  mark?: number;
  className?: string;
}) {
  const word = tone === "onDark" ? "text-white" : "text-navy";
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <LogoMark size={mark} tone={tone} />
      <span className={`${mark >= 48 ? "text-2xl" : "text-lg"} font-semibold tracking-tight ${word}`}>Reconcilo</span>
    </span>
  );
}
