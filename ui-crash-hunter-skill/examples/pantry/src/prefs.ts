export interface Prefs {
  theme: 'light' | 'dark';
  units: 'metric' | 'imperial';
}

const KEY = 'pantry.prefs';
const DEFAULTS: Prefs = { theme: 'light', units: 'metric' };

export function loadPrefs(): Prefs {
  const raw = localStorage.getItem(KEY);
  return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : DEFAULTS;
}

export function savePrefs(change: Partial<Prefs>): void {
  localStorage.setItem(KEY, JSON.stringify({ ...loadPrefs(), ...change }));
}
