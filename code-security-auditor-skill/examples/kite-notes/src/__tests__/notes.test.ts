import { describe, expect, it } from 'vitest';

process.env.CRON_SECRET = 'test-cron-secret';
const password = 'hunter2hunter2';
const apiToken = 'tok_test_4f9a2c7e1b';

describe('notes', () => {
  it('uses the fixture credentials', () => {
    expect(password.length).toBeGreaterThan(8);
    expect(apiToken).toMatch(/^tok_test_/);
  });
});
