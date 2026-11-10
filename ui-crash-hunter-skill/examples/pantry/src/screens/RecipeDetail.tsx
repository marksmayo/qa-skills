import { useEffect, useState } from 'react';
import { getRecipe } from '../api';
import { NutritionPanel } from '../components/NutritionPanel';
import { PanelBoundary } from '../components/PanelBoundary';
import type { Params } from '../routes';
import type { Recipe } from '../types';

export function RecipeDetail({ params }: { params: Params }) {
  const [recipe, setRecipe] = useState<Recipe | null>(null);

  useEffect(() => {
    getRecipe(params.id).then(setRecipe);
  }, [params.id]);

  if (!recipe) return <p>Loading…</p>;

  const byAisle = Object.groupBy(recipe.ingredients, (i) => i.aisle ?? 'Other');

  return (
    <article>
      <h1>{recipe.title}</h1>
      <p className="meta">
        {recipe.author?.displayName ?? 'Imported recipe'} · {recipe.rating.toFixed(1)} ★ ({recipe.ratingCount})
      </p>
      <h2>Ingredients</h2>
      {Object.entries(byAisle).map(([aisle, items]) => (
        <section key={aisle}>
          <h3>{aisle}</h3>
          <ul>
            {items.map((i) => (
              <li key={i.name}>
                {i.quantity === null ? 'To taste' : `${i.quantity} ${i.unit ?? ''}`} {i.name}
              </li>
            ))}
          </ul>
        </section>
      ))}
      <PanelBoundary>
        <NutritionPanel nutrition={recipe.nutrition} />
      </PanelBoundary>
      <h2>Method</h2>
      <ol>
        {recipe.steps.map((s, n) => (
          <li key={n}>{s}</li>
        ))}
      </ol>
    </article>
  );
}
