import { expect, it } from "vitest";
import { renderCheckout } from "./checkout";

const booking = { route: "circular-quay:manly", passengers: 3, total_cents: 2450 };

it("shows the route and the party size", () => {
  const html = renderCheckout(booking);
  expect(html).toContain("<h2>circular-quay to manly</h2>");
  expect(html).toContain("3 passengers");
});

it("puts the total on the pay button", () => {
  expect(renderCheckout(booking)).toContain("Pay $24.50");
});
