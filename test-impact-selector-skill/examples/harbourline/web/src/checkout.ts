import "./checkout.css";
import { formatFare, formatParty } from "./fare";

export interface Booking {
  route: string;
  passengers: number;
  total_cents: number;
}

export function renderCheckout(booking: Booking): string {
  return [
    `<section class="checkout">`,
    `<h2>${booking.route.replace(":", " to ")}</h2>`,
    `<p>${formatParty(booking.passengers)}</p>`,
    `<p class="total">Total ${formatFare(booking.total_cents)}</p>`,
    `<button type="submit">Pay ${formatFare(booking.total_cents)}</button>`,
    `</section>`,
  ].join("\n");
}
