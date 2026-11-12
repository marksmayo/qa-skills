import { prisma } from './db';

export async function placeOrder(customerId: string, total: number) {
  return prisma.order.create({
    data: { customerId, total },
  });
}

export async function placeGiftOrder(customerId: string, total: number, giftNote: string) {
  return prisma.order.create({
    data: { customerId, total, giftNote, channel: 'web' },
  });
}

export async function importOrder(payload: { customerId: string; total: number }) {
  return prisma.order.create({ data: payload });
}

export async function recentOrders() {
  return prisma.order.findMany({ orderBy: { createdAt: 'desc' }, take: 20 });
}
