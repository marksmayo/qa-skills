import { useEffect, useState } from 'react';
import { listRecipes } from '../api';
import { Stars } from '../components/Stars';
import type { RecipeSummary } from '../types';

export function RecipeList() {
  const [recipes, setRecipes] = useState<RecipeSummary[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    listRecipes().then(setRecipes, () => setFailed(true));
  }, []);

  if (failed) return <p role="alert">Couldn't load recipes.</p>;
  if (!recipes) return <p>Loading…</p>;

  return (
    <ul className="recipes">
      {recipes.map((r) => (
        <li key={r.id}>
          <a href={`#/recipes/${r.id}`}>{r.title}</a> <span className="by">by {r.author.displayName}</span>{' '}
          <Stars rating={r.rating} count={r.ratingCount} />
        </li>
      ))}
    </ul>
  );
}
