import type { Recipe, RecipeSummary } from './types';

async function get<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`GET ${url} failed: ${res.status}`);
  return (await res.json()) as T;
}

export const listRecipes = () => get<RecipeSummary[]>('/api/recipes');
export const getRecipe = (id: string) => get<Recipe>(`/api/recipes/${encodeURIComponent(id)}`);
