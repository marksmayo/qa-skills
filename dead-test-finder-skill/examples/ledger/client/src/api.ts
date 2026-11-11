export interface DraftEntry {
  account: string;
  amountCents: number;
}

export async function save(entry: DraftEntry): Promise<boolean> {
  if (entry.amountCents === 0) {
    return false;
  }
  return true;
}

export function formatCents(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${sign}$${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}
