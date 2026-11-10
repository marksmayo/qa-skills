import type { Nutrition } from '../types';

export function NutritionPanel({ nutrition }: { nutrition?: Nutrition }) {
  const perServing = Math.round(nutrition.calories / nutrition.servings);
  return (
    <section>
      <h2>Nutrition</h2>
      <p>
        {perServing} kcal and {nutrition.protein} g protein per serving
      </p>
    </section>
  );
}
