import { expect, it } from "vitest";
import { compactHeader } from "./compact";

it("puts the reference in the header", () => {
  expect(compactHeader({ reference: "HL-2041" })).toBe("HARBOURLINE HL-2041");
});
