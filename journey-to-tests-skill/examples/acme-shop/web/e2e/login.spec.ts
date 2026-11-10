import { test, expect } from '@playwright/test';
import { seedMember } from './support/seed';

test('member signs in @smoke', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('Email').fill('member@example.test');
  await page.getByLabel('Password').fill(process.env.TEST_MEMBER_PASSWORD!);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page).toHaveURL('/account');
});

// Added by Journey to Tests. Journey: journeys/account/JRN-ACC-001-member-signs-in-and-views-their-account.md
test.describe('JRN-ACC-001 Member signs in and views their account', { tag: ['@JRN-ACC-001', '@P1'] }, () => {
  test('JRN-ACC-001 main path: member sees their name and past orders', async ({ page, request }) => {
    const member = await seedMember(request, { orders: 1 });

    // Step 1
    await page.goto('/login');
    await expect(page.getByLabel('Email')).toBeVisible();
    await expect(page.getByLabel('Password')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();

    // Step 2
    await page.getByLabel('Email').fill(member.email);
    await page.getByLabel('Password').fill(member.password);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page).toHaveURL('/account');

    // Step 3 (order history list: see testability gap GAP-001)
    await expect(page.getByRole('heading', { name: member.name })).toBeVisible();
  });

  test('JRN-ACC-001 V1 wrong password: error shown, user stays on /login', async ({ page, request }) => {
    const member = await seedMember(request, {});
    await page.goto('/login');
    await page.getByLabel('Email').fill(member.email);
    await page.getByLabel('Password').fill('not-the-password');
    await page.getByRole('button', { name: 'Sign in' }).click();

    await expect(page.getByRole('alert')).toHaveText('Email or password is incorrect');
    await expect(page).toHaveURL('/login');
  });

  test('JRN-ACC-001 V2 account locked: locked message with reset link', async ({ page, request }) => {
    const member = await seedMember(request, { locked: true });
    await page.goto('/login');
    await page.getByLabel('Email').fill(member.email);
    await page.getByLabel('Password').fill(member.password);
    await page.getByRole('button', { name: 'Sign in' }).click();

    await expect(page.getByRole('alert')).toContainText('locked');
    await expect(page.getByRole('link', { name: 'Reset your password' })).toBeVisible();
  });
});
