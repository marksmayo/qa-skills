// Date helpers. Dates are stored as YYYY-MM-DD strings in the user's local calendar.

export function localDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function addDays(d, n) {
  const copy = new Date(d.getTime());
  copy.setDate(copy.getDate() + n);
  return copy;
}

export function isDueToday(task, now) {
  const today = now.toISOString().slice(0, 10);
  return task.due === today;
}
