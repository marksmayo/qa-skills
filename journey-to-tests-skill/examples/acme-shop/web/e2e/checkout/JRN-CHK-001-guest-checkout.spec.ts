import { test, expect } from '@playwright/test';
import { CheckoutPage } from '../pages/CheckoutPage';
import { seedProduct } from '../support/seed';

// Journey: journeys/checkout/JRN-CHK-001-guest-completes-checkout-with-card.md
// Card numbers are the payment provider's published sandbox test cards (journey "Test data").
const SHIPPING = { email: 'guest@example.test', address: '1 Test Street', city: 'Sydney', postcode: '2000' };

test.describe('JRN-CHK-001 Guest completes checkout with card', { tag: ['@JRN-CHK-001', '@P1'] }, () => {
  test('JRN-CHK-001 main path: guest pays by card and sees an order number', async ({ page, request }) => {
    const product = await seedProduct(request, { stock: 5 });
    const checkout = new CheckoutPage(page);

    // Step 1
    await page.goto('/');
    await page.getByRole('link', { name: product.title }).click();
    await expect(page.getByRole('heading', { name: product.title })).toBeVisible();
    await expect(page.getByText(product.priceLabel)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Add to cart' })).toBeEnabled();

    // Step 2
    await page.getByRole('button', { name: 'Add to cart' }).click();
    await expect(page.getByRole('link', { name: /cart/i })).toContainText('1');

    // Step 3
    await page.getByRole('link', { name: /cart/i }).click();
    await page.getByRole('link', { name: 'Checkout' }).click();
    await expect(checkout.address).toBeVisible();
    await expect(checkout.card.getByPlaceholder('Card number')).toBeVisible();
    await expect(checkout.payNow).toBeDisabled();

    // Step 4
    await checkout.fillShipping(SHIPPING);
    await checkout.fillCard('4242 4242 4242 4242');
    await expect(checkout.payNow).toBeEnabled();

    // Step 5
    await checkout.payNow.click();
    await expect(page).toHaveURL(/\/checkout\/confirmation/);
    await expect(page.getByText(/Order number:\s*\w+/)).toBeVisible();
  });

  test('JRN-CHK-001 V1 card declined: inline error, user stays on checkout', async ({ page, request }) => {
    const product = await seedProduct(request, { stock: 5 });
    const checkout = new CheckoutPage(page);
    await page.goto(`/products/${product.slug}`);
    await page.getByRole('button', { name: 'Add to cart' }).click();
    await checkout.goto();
    await checkout.fillShipping(SHIPPING);
    await checkout.fillCard('4000 0000 0000 0002');
    await checkout.payNow.click();

    await expect(checkout.paymentError).toHaveText('Your card was declined');
    await expect(page).toHaveURL(/\/checkout$/);
  });

  test('JRN-CHK-001 V2 out of stock: Add to cart is disabled', async ({ page, request }) => {
    const product = await seedProduct(request, { stock: 0 });
    await page.goto(`/products/${product.slug}`);

    const addToCart = page.getByRole('button', { name: /Add to cart|Out of stock/ });
    await expect(addToCart).toBeDisabled();
    await expect(addToCart).toHaveText('Out of stock');
  });
});
