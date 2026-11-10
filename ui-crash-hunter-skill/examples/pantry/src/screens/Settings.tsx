import { useState } from 'react';
import { loadPrefs, savePrefs, type Prefs } from '../prefs';

export function Settings() {
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs);

  function update(change: Partial<Prefs>) {
    savePrefs(change);
    setPrefs({ ...prefs, ...change });
  }

  return (
    <form onSubmit={(e) => e.preventDefault()}>
      <h1>Settings</h1>
      <label>
        Theme{' '}
        <select value={prefs.theme} onChange={(e) => update({ theme: e.target.value as Prefs['theme'] })}>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </label>
      <label>
        Units{' '}
        <select value={prefs.units} onChange={(e) => update({ units: e.target.value as Prefs['units'] })}>
          <option value="metric">Metric</option>
          <option value="imperial">Imperial</option>
        </select>
      </label>
    </form>
  );
}
