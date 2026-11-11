export function formatFare(cents: number): string {
  const dollars = Math.floor(cents / 100);
  const rest = String(cents % 100).padStart(2, "0");
  return `$${dollars}.${rest}`;
}

export function formatParty(passengers: number): string {
  return passengers === 1 ? "1 passenger" : `${passengers} passengers`;
}
