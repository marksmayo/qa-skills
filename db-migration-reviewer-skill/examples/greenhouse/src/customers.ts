import { prisma } from './db';

export async function contactDetails(id: string) {
  return prisma.customer.findUnique({
    where: { id },
    select: { email: true, phone: true },
  });
}

export async function findByImportRef(ref: string) {
  return prisma.customer.findFirst({ where: { legacyRef: ref } });
}
