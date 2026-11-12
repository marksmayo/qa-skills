import { NextResponse } from 'next/server';
import { prisma } from '@/lib/prisma';

export async function POST(request: Request) {
  const event = await request.json();
  if (event.type === 'invoice.paid') {
    await prisma.team.update({ where: { id: event.data.teamId }, data: { plan: 'pro' } });
  }
  return NextResponse.json({ received: true });
}
