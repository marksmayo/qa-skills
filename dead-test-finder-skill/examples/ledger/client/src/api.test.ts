import { describe, expect, it } from "vitest";
import { formatCents, save } from "./api";

describe("save", () => {
  it("saves a valid entry", () => {
    expect(save({ account: "sales", amountCents: 12000 })).resolves.toBe(true);
  });

  it("refuses a zero entry", async () => {
    await expect(save({ account: "sales", amountCents: 0 })).resolves.toBe(false);
  });
});

describe("formatCents", () => {
  it("formats negative amounts", () => {
    expect(formatCents(-1505)).toBe("-$15.05");
  });
});
