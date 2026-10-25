import { useId } from "react";

/**
 * Reconcilo mark: an outlined coin, a geometric R, and rising ledger bars.
 * On navy the coin is a white line with a faint fill and a soft glow.
 * On the pale canvas the same drawing is navy so it stays readable.
 */
export function LogoMark({
  size = 32,
  tone = "onDark",
  working = false,
  float = false,
  title = "Reconcilo",
}: {
  size?: number;
  tone?: "onDark" | "onLight";
  working?: boolean;
  float?: boolean;
  title?: string;
}) {
  const onDark = tone === "onDark";
  const ink = onDark ? "#F7FBFA" : "#071833";
  const glowId = `coin-glow-${useId().replace(/:/g, "")}`;
  const motion = float || working ? "logo-float shrink-0" : "shrink-0";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label={title}
      className={motion}
      overflow="visible"
    >
      {onDark && (
        <defs>
          <filter id={glowId} x="-50%" y="-50%" width="200%" height="200%" colorInterpolationFilters="sRGB">
            <feGaussianBlur stdDeviation="1.8" />
          </filter>
        </defs>
      )}
      <g opacity={onDark ? undefined : 1}>
        {onDark && <circle cx="32" cy="32" r="27.5" fill={ink} opacity="0.28" filter={`url(#${glowId})`} />}
        <circle cx="32" cy="32" r="29.2" fill={ink} fillOpacity={onDark ? 0.1 : 0.05} />
        <circle cx="32" cy="32" r="29.2" fill="none" stroke={ink} strokeWidth="1.7" />
        <circle cx="32" cy="32" r="24.2" fill="none" stroke={ink} strokeWidth="1.15" opacity={onDark ? 0.62 : 0.4} />
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
      </g>
    </svg>
  );
}

export function Logo({
  tone = "onDark",
  mark = 36,
  float = false,
  className = "",
}: {
  tone?: "onDark" | "onLight";
  mark?: number;
  float?: boolean;
  className?: string;
}) {
  const word = tone === "onDark" ? "text-white" : "text-navy";
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <LogoMark size={mark} tone={tone} float={float} />
      <span className={`${mark >= 48 ? "text-2xl" : "text-lg"} font-semibold tracking-tight ${word}`}>Reconcilo</span>
    </span>
  );
}
