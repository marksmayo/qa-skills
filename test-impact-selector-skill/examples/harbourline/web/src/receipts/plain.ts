import { formatFare } from "../fare";
import { register } from "./index";

register("plain", (booking) =>
  `Booking ${booking.reference}\n${booking.route}\nPaid ${formatFare(booking.total_cents)}`,
);
