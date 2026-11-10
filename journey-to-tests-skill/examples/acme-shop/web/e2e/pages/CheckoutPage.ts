import { type Page, type Locator, type FrameLocator } from '@playwright/test';

// Added by Journey to Tests for JRN-CHK-001. Follows the existing page object style in this folder.
export class CheckoutPage {
  readonly page: Page;
  readonly email: Locator;
  readonly address: Locator;
  readonly city: Locator;
  readonly postcode: Locator;
  readonly card: FrameLocator;
  readonly payNow: Locator;
  readonly paymentError: Locator;

  constructor(page: Page) {
    this.page = page;
    this.email = page.getByLabel('Email');
    this.address = page.getByLabel('Address');
    this.city = page.getByLabel('City');
    this.postcode = page.getByLabel('Postcode');
    // Card fields are rendered by the payment provider inside an iframe.
    this.card = page.frameLocator('iframe[title="Secure card payment input frame"]');
    this.payNow = page.getByRole('button', { name: 'Pay now' });
    this.paymentError = page.getByRole('alert');
  }

  async goto() {
    await this.page.goto('/checkout');
  }

  async fillShipping(details: { email: string; address: string; city: string; postcode: string }) {
    await this.email.fill(details.email);
    await this.address.fill(details.address);
    await this.city.fill(details.city);
    await this.postcode.fill(details.postcode);
  }

  async fillCard(number: string) {
    await this.card.getByPlaceholder('Card number').fill(number);
    await this.card.getByPlaceholder('MM / YY').fill('12 / 34');
    await this.card.getByPlaceholder('CVC').fill('123');
  }
}
