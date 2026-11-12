import { NextResponse } from 'next/server';
import { prisma } from '@/lib/prisma';

function authorised(request: Request) {
  return request.headers.get('authorization') === `Bearer ${process.env.CRON_SECRET}`;
}

async function purge(request: Request) {
  if (!authorised(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }
  const cutoff = new Date(Date.now() - 30 * 24 * 3600 * 1000);
  const { count } = await prisma.note.deleteMany({ where: { deletedAt: { lt: cutoff } } });
  return NextResponse.json({ purged: count });
}

export const GET = (request: Request) => purge(request);
