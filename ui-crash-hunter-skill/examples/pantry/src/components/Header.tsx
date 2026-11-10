import { loadPrefs } from '../prefs';

export function Header() {
  const prefs = loadPrefs();
  return (
    <header className={`theme-${prefs.theme}`}>
      <a href="#/">Pantry</a>
      <a href="#/settings">Settings</a>
    </header>
  );
}
