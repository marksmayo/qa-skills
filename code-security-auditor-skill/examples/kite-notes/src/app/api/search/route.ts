import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/prisma';
import { getCurrentUser } from '@/lib/auth';

export async function GET(request: NextRequest) {
  const user = await getCurrentUser();
  if (!user) {
    return NextResponse.json({ error: 'Sign in first' }, { status: 401 });
  }
  const term = request.nextUrl.searchParams.get('q') ?? '';
  const sql = `SELECT id, title FROM "Note" WHERE "ownerId" = '${user.id}' AND title ILIKE '%${term}%'`;
  const rows = await prisma.$queryRawUnsafe(sql);
  return NextResponse.json(rows);
}
