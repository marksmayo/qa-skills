// Mirrors openapi.json. Nullable and optional fields follow the contract.

export interface Author {
  id: string;
  displayName: string;
  avatarUrl?: string;
}

export interface RecipeSummary {
  id: string;
  title: string;
  /** null for recipes imported from a URL */
  author: Author | null;
  /** null until the recipe has at least one rating */
  rating: number | null;
  ratingCount: number;
  tags: string[];
}

export interface Ingredient {
  name: string;
  /** null for "to taste" */
  quantity: number | null;
  unit?: string;
  aisle?: string;
}

export interface Nutrition {
  calories: number;
  protein: number;
  servings: number;
}

export interface Recipe extends RecipeSummary {
  ingredients: Ingredient[];
  steps: string[];
  publishedAt: string;
  /** only present once the nutrition job has run */
  nutrition?: Nutrition;
}
