export interface ReceiptBooking {
  reference: string;
  route: string;
  total_cents: number;
}

type Renderer = (booking: ReceiptBooking) => string;

const renderers = new Map<string, Renderer>();

export function register(name: string, render: Renderer): void {
  renderers.set(name, render);
}

export function renderReceipt(name: string, booking: ReceiptBooking): string {
  const render = renderers.get(name);
  if (!render) {
    throw new Error(`no receipt style ${name}`);
  }
  return render(booking);
}
