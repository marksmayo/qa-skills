import { prisma } from './db';

export async function priceList() {
  return prisma.plant.findMany({ select: { name: true, price: true, weightKg: true } });
}
