import { expect, it } from "vitest";
import css from "../src/checkout.css?raw";
import { renderCheckout } from "../src/checkout";

it("checkout markup and styles match the approved snapshot", () => {
  const html = renderCheckout({ route: "circular-quay:manly", passengers: 2, total_cents: 1960 });
  expect(`<style>${css}</style>\n${html}`).toMatchSnapshot();
});
