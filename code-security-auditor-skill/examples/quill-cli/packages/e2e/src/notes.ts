import { readFileSync } from 'node:fs';

// planted: the MCP tool's path argument is read as is (no check that it stays in the notes folder)
export function readNote(path: string): string {
  return readFileSync(path, 'utf8');
}

/** Pushes a note to the sync service named in the settings (no request object here). */
export async function pushNote(settings: { apiUrl: string }, route: string, body: string) {
  return fetch(`${settings.apiUrl}${route}`, { method: 'POST', body });
}
