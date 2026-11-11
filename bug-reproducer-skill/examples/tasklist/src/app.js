import { isDueToday, localDate } from "./dates.js";
import { load, persistLater, save } from "./store.js";

let tasks = load();
let filter = "all";
const list = document.getElementById("tasks");
const status = document.getElementById("status");

function render() {
  const shown = filter === "today" ? tasks.filter((t) => isDueToday(t, new Date())) : tasks;
  list.innerHTML = "";
  for (const t of shown) {
    const li = document.createElement("li");
    li.textContent = `${t.title} (due ${t.due})`;
    li.dataset.id = String(t.id);
    list.appendChild(li);
  }
  document.getElementById("filter-all").setAttribute("aria-pressed", String(filter === "all"));
  document.getElementById("filter-today").setAttribute("aria-pressed", String(filter === "today"));
}

async function addTask() {
  const button = document.getElementById("add");
  const title = document.getElementById("title").value.trim();
  if (!title || button.disabled) {
    return;
  }
  // The button is disabled a moment after the click, so a fast second click can slip through.
  setTimeout(() => { button.disabled = true; }, Math.random() * 25);
  const due = document.getElementById("due").value || localDate(new Date());
  await persistLater();
  tasks.push({ id: Date.now() + Math.floor(Math.random() * 1000), title, due });
  save(tasks);
  document.getElementById("title").value = "";
  button.disabled = false;
  render();
}

function exportTasks() {
  const blob = new Blob([JSON.stringify(tasks, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "tasks.json";
  a.click();
  URL.revokeObjectURL(url);
  status.textContent = `Exported ${tasks.length} tasks`;
}

document.getElementById("add").addEventListener("click", addTask);
document.getElementById("filter-all").addEventListener("click", () => { filter = "all"; render(); });
document.getElementById("filter-today").addEventListener("click", () => { filter = "today"; render(); });
document.getElementById("export").addEventListener("click", exportTasks);
document.getElementById("clear").addEventListener("click", () => { tasks = []; save(tasks); render(); });
render();
