import { addDays, localDate } from "./dates.js";

const KEY = "tasklist.tasks";

export function load() {
  const raw = localStorage.getItem(KEY);
  if (raw) {
    return JSON.parse(raw);
  }
  const now = new Date();
  const seed = [
    { id: 1, title: "Pay the water bill", due: localDate(addDays(now, -1)) },
    { id: 2, title: "Book the car service", due: localDate(now) },
    { id: 3, title: "Return library books", due: localDate(addDays(now, 1)) },
  ];
  save(seed);
  return seed;
}

export function save(tasks) {
  localStorage.setItem(KEY, JSON.stringify(tasks));
}

export function persistLater() {
  // Stands in for a network round trip to the server.
  return new Promise((resolve) => setTimeout(resolve, 40));
}
