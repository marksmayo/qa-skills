import { describe, expect, it } from 'vitest';
import { prisma } from '../src/db';

describe('orders', () => {
  it('stores an order', async () => {
    const order = await prisma.order.create({ data: { customerId: 'cust-1', total: 12.5 } });
    expect(order.total).toBeDefined();
  });
});
