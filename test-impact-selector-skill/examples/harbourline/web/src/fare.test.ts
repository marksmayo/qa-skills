import { describe, expect, it } from "vitest";
import { formatFare, formatParty } from "./fare";

describe("formatFare", () => {
  it("formats whole dollars", () => {
    expect(formatFare(900)).toBe("$9.00");
  });
  it("pads cents", () => {
    expect(formatFare(1205)).toBe("$12.05");
  });
});

describe("formatParty", () => {
  it("uses the singular for one passenger", () => {
    expect(formatParty(1)).toBe("1 passenger");
  });
  it("uses the plural otherwise", () => {
    expect(formatParty(3)).toBe("3 passengers");
  });
});
