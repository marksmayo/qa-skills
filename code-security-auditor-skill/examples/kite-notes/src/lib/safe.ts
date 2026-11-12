// Safe counterparts of the planted weaknesses: the scanners must stay quiet here.
import { prisma } from './prisma';

export function safeReturnPath(value: string) {
  if (value.startsWith('/') && !value.startsWith('//') && !value.startsWith('/\\')) {
    return value;
  }
  return '/';
}

export async function noteCount(ownerId: string) {
  return prisma.$queryRaw`SELECT count(*) FROM "Note" WHERE "ownerId" = ${ownerId}`;
}

export function csvCell(value: string) {
  const safe = /^[=+\-@\t\r]/.test(value) ? `'${value}` : value;
  return `"${safe.replace(/"/g, '""')}"`;
}

export function csvRow(values: string[]) {
  return values.map(csvCell).join(',');
}
