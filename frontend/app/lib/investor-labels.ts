const INVESTOR_LABELS: Record<string, string> = {
  "SUPPORT:BALANCE_SHEET": "Balance sheet strength",
  "SUPPORT:FUNDAMENTAL_BUSINESS_QUALITY": "Business quality",
  "SUPPORT:GROWTH": "Growth",
  "SUPPORT:SHAREHOLDING": "Shareholding",
  "UNAVAILABLE:NEWS_GEOPOLITICAL_EVENTS": "News and geopolitical evidence unavailable",
  "UNAVAILABLE:SECTOR": "Sector evidence unavailable"
};

function titleCaseCode(value: string): string {
  const words = value.replaceAll("_", " ").toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : "Unavailable";
}

/** Maps an internal evidence/action code for display without changing the
 * persisted value held by the domain object. */
export function investorLabel(value: string | undefined): string {
  if (!value) return "Unavailable";
  const exact = INVESTOR_LABELS[value];
  if (exact) return exact;
  const separator = value.indexOf(":");
  if (separator > 0) {
    const category = value.slice(0, separator);
    const detail = titleCaseCode(value.slice(separator + 1));
    if (category === "UNAVAILABLE") return `${detail} evidence unavailable`;
    if (category === "SUPPORT") return detail;
  }
  return titleCaseCode(value);
}
