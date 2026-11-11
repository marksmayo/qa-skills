import { formatFare } from "../fare";
import { register } from "./index";

export function compactHeader(booking: { reference: string }): string {
  return `HARBOURLINE ${booking.reference}`;
}

register("compact", (booking) =>
  [compactHeader(booking), booking.route, formatFare(booking.total_cents), "Show this at the gate"].join(" | "),
);
