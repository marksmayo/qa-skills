import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export interface Settings {
  packageManager?: string;
  apiUrl?: string;
  notesDir?: string;
}

/** Reads quill.config.json from the directory the command runs in. */
export function loadSettings(dir: string): Settings {
  const configPath = join(dir, 'quill.config.json');
  return JSON.parse(readFileSync(configPath, 'utf8')) as Settings;
}

/** Version banner lines; the regular expression's exec is not a process. */
export function banner(text: string): string | undefined {
  const found = /^version:\s*(\S+)/mu.exec(text + '\n');
  return found?.[1];
}
