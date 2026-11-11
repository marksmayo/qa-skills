import { expect, it } from "vitest";
import { renderCheckout } from "../src/checkout";
import { renderReceipt } from "../src/receipts/index";

it("a family books Circular Quay to Manly and gets a compact receipt", () => {
  const booking = { reference: "HL-2041", route: "circular-quay:manly", passengers: 3, total_cents: 2450 };
  const page = renderCheckout(booking);
  expect(page).toContain("Total $24.50");
  const receipt = renderReceipt("compact", booking);
  expect(receipt).toBe("HARBOURLINE HL-2041 | circular-quay:manly | $24.50 | Show this at the gate");
});
