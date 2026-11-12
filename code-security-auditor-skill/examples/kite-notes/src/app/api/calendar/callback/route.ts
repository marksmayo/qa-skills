import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/prisma';

export async function GET(request: NextRequest) {
  const code = request.nextUrl.searchParams.get('code');
  const state = request.nextUrl.searchParams.get('state');
  if (!code || !state) {
    return NextResponse.json({ error: 'Missing code' }, { status: 400 });
  }
  const { teamId } = JSON.parse(Buffer.from(state, 'base64url').toString('utf8'));
  await prisma.calendarLink.upsert({
    where: { teamId },
    create: { teamId, code },
    update: { code },
  });
  return NextResponse.redirect(new URL('/settings', request.url));
}
