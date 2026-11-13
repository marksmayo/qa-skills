import { execFileSync } from 'node:child_process';
import { expect, test } from 'vitest';

const CLI = process.env.QUILL_CLI ?? 'quill';

test('prints its version', () => {
  // test code: a variable command here is not a candidate
  expect(execFileSync(CLI, ['version'], { encoding: 'utf8' })).toMatch(/\d+\.\d+/);
});
