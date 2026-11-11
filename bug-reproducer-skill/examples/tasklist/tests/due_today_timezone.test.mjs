import { test } from "node:test";
import assert from "node:assert/strict";
import { isDueToday } from "../src/dates.js";

// TASK-11: before UTC midnight, users east of UTC see yesterday's tasks under "Due today".
process.env.TZ = "Australia/Sydney";

test("TASK-11: at 8am in Sydney, a task due today is due today", () => {
  const now = new Date("2026-11-12T08:00:00+11:00");
  assert.equal(isDueToday({ due: "2026-11-12" }, now), true);
});

test("TASK-11: at 8am in Sydney, a task due yesterday is not due today", () => {
  const now = new Date("2026-11-12T08:00:00+11:00");
  assert.equal(isDueToday({ due: "2026-11-11" }, now), false);
});
