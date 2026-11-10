import type { ComponentType } from 'react';
import { RecipeList } from './screens/RecipeList';
import { RecipeDetail } from './screens/RecipeDetail';
import { Settings } from './screens/Settings';

export type Params = Record<string, string>;

interface Route {
  path: string;
  component: ComponentType<{ params: Params }>;
}

export const routes: Route[] = [
  { path: '/', component: RecipeList },
  { path: '/recipes/:id', component: RecipeDetail },
  { path: '/settings', component: Settings },
];

export function match(path: string): { route: Route; params: Params } {
  const parts = path.split('/').filter(Boolean);
  for (const route of routes) {
    const pattern = route.path.split('/').filter(Boolean);
    if (pattern.length !== parts.length) continue;
    const params: Params = {};
    const ok = pattern.every((p, i) => {
      if (p.startsWith(':')) {
        params[p.slice(1)] = decodeURIComponent(parts[i]);
        return true;
      }
      return p === parts[i];
    });
    if (ok) return { route, params };
  }
  return { route: routes[0], params: {} };
}
