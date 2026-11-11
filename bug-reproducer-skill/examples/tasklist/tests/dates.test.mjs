import { test } from "node:test";
import assert from "node:assert/strict";
import { addDays, localDate, isDueToday } from "../src/dates.js";

test("localDate formats the local calendar date", () => {
  assert.equal(localDate(new Date(2026, 0, 5, 9, 30)), "2026-01-05");
});

test("addDays crosses a month end", () => {
  assert.equal(localDate(addDays(new Date(2026, 0, 31), 1)), "2026-02-01");
});

test("a task due today is due today", () => {
  const now = new Date();
  assert.equal(isDueToday({ due: localDate(now) }, now), true);
});
