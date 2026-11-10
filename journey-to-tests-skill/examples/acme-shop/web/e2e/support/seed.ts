import { type APIRequestContext } from '@playwright/test';

// Existing seeding helpers (test-only API, enabled when ENABLE_TEST_SEED=1).
export async function seedProduct(request: APIRequestContext, opts: { stock: number }) {
  const res = await request.post('/api/test/seed/product', { data: opts });
  return res.json();
}

export async function seedMember(request: APIRequestContext, opts: { orders?: number; locked?: boolean }) {
  const res = await request.post('/api/test/seed/member', { data: opts });
  return res.json();
}
