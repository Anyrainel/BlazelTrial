export const AGENT_NAME = "Hazel";

function Logo() {
  return (
    <svg className="brand-logo" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="9" fill="var(--accent)" />
      <path
        d="M16 7c-4.4 0-8 3.4-8 7.6 0 2.4 1.1 4.5 2.9 5.9L10 25l4.3-2.4c.6.1 1.1.2 1.7.2 4.4 0 8-3.4 8-7.6S20.4 7 16 7z"
        fill="#fff"
      />
      <circle cx="12.5" cy="14.8" r="1.2" fill="var(--accent)" />
      <circle cx="19.5" cy="14.8" r="1.2" fill="var(--accent)" />
    </svg>
  );
}

/** Top-left brand mark shown on every screen: logo, product name and the Demo badge. */
export function Brand() {
  return (
    <div className="brand">
      <Logo />
      <span className="brand-name">{AGENT_NAME}</span>
      <span className="brand-badge">Demo</span>
    </div>
  );
}
