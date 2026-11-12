import { beforeEach } from 'vitest';

beforeEach(() => {
  const params = new URLSearchParams({ page: '1' });
  process.env.TEST_QUERY = params.toString();
});
